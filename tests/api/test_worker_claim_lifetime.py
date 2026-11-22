import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import select
from starlette.types import Message

from kora.api.exchange import ApiExchange
from kora.api.installed_worker import installed_worker_profile
from kora.cel_eval.thread import Evaluator
from kora.client import ClientError, KoraClient
from kora.client.errors import ApiError, Failure, Outcome
from kora.conformance.capture import captured
from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt_dispatcher
from kora.engine.locks import TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    namespace_policy_scopes,
    write_namespace_policy,
)
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from tests.api.claim_restart_process import claim_process, lose_claim_response
from tests.api.claim_support import (
    BEARER,
    LIMITS,
    authorize_claim_worker,
    claim_api,
    claim_application,
    request,
)
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.api.support import HttpCall, lifespan
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.engine.claim_support import ClaimCase, another_run, claim_case, state
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["claim_api", "evaluator"]


@dataclass
class HeldClaimResponse(HttpCall):
    payload: bytes = b""
    lose_response: bool = False
    sending: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def receive(self) -> Message:
        self.receives += 1
        return {"type": "http.request", "body": self.payload, "more_body": False}

    async def send(self, message: Message) -> None:
        await super().send(message)
        if message["type"] == "http.response.start":
            self.sending.set()
            await self.release.wait()
            if self.lose_response:
                raise ConnectionResetError("claim response receiver disconnected")


@pytest_asyncio.fixture
async def claim_response_app(
    database: Database, evaluator: Evaluator, single_claim: bool
) -> AsyncGenerator[tuple[ClaimCase, ApiExchange]]:
    case = await claim_case(database, evaluator)
    await authorize_claim_worker(database, case)
    profile = installed_worker_profile("2000")
    assert profile is not None
    clock = AuthorityClock()
    with captured("api", LoggingSettings(), "claim-response", clock.now) as capture:
        yield (
            case,
            claim_application(
                database,
                capture,
                clock,
                limits=profile.limits if single_claim else LIMITS,
                single_claim=single_claim,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receiver", "single_claim"),
    [
        pytest.param("slow", False, id="generic-slow"),
        pytest.param("lost", False, id="generic-lost"),
        pytest.param("cancelled", False, id="generic-cancelled"),
        pytest.param("lost", True, id="installed-slot-lost"),
        pytest.param("cancelled", True, id="installed-slot-cancelled"),
    ],
)
async def test_claim_settles_before_response_delivery_and_replays_after_loss(
    database: Database,
    claim_response_app: tuple[ClaimCase, ApiExchange],
    receiver: str,
    single_claim: bool,
) -> None:
    case, app = claim_response_app
    call = HeldClaimResponse(
        path="/api/v1/workers/claim-worker/claims",
        method="POST",
        headers=[
            (b"authorization", ("Bearer " + BEARER).encode()),
            (b"content-type", b"application/json"),
        ],
        payload=request(case).model_dump_json().encode(),
        lose_response=receiver == "lost",
    )
    ledger = ConnectionLedger(database)

    replayed = None
    pending = asyncio.create_task(call.run(app))
    try:
        async with asyncio.timeout(10):
            await call.sending.wait()
            held_units = database.units_in_flight()
            held_connections = ledger.outstanding
            committed = await state(database)
            async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
                if single_claim:
                    with pytest.raises(ApiError) as busy:
                        await client.claim_worker(case.query.worker_id, request(case))
                    assert busy.value.status == 503
                    assert busy.value.code == "API011"
                    assert await state(database) == committed
                else:
                    replayed = await client.claim_worker(
                        case.query.worker_id, request(case)
                    )
            if receiver == "cancelled":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif receiver == "lost":
                call.release.set()
                with pytest.raises(ConnectionResetError):
                    await pending
            else:
                call.release.set()
                await pending
            async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
                after_delivery = await client.claim_worker(
                    case.query.worker_id, request(case)
                )
        after = await state(database)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    assert call.status == 200
    assert held_units == held_connections == 0
    assert len(committed["attempt_dispatcher"]) == 1
    assert committed == after
    assert after_delivery.claim is not None
    assert after_delivery.claim.attempt_id == case.binding.attempt_id
    if not single_claim:
        assert replayed == after_delivery
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_lost_http_claim_recovers_original_grant_after_api_process_restart(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1h"}
    case = await claim_case(database, evaluator, ir, capacity=2)
    tail = await another_run(database, case)
    original_request = request(case)
    namespace_id = case.binding.run.namespace_id
    later = START + timedelta(seconds=20)
    original_stall = timedelta(microseconds=19)
    next_stall = timedelta(seconds=4, microseconds=23)
    await authorize_claim_worker(database, case)

    async with claim_process(database.settings, START, original_stall) as first_process:
        async with lose_claim_response(first_process.origin) as lost:
            async with KoraClient(lost.origin, BEARER) as client:
                with pytest.raises(ClientError) as lost_response:
                    await client.claim_worker(case.query.worker_id, original_request)
            original = lost.original.result()
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(namespace_policy_scopes(namespace_id))
        await write_namespace_policy(
            writer,
            locks,
            NamespacePolicyConfiguration.prepare(
                namespace_id, 3, {"require_protected_execution": False}
            ),
            later,
        )
    before_replay = await state(database)
    async with claim_process(database.settings, later, next_stall) as second_process:
        async with KoraClient(second_process.origin, BEARER) as client:
            replay = await client.claim_worker(case.query.worker_id, original_request)
            after_replay = await state(database)
            fresh = await client.claim_worker(tail.query.worker_id, request(tail))
    after_fresh = await state(database)

    assert first_process.pid != second_process.pid
    assert lost_response.value.reason is Failure.STREAM
    assert lost_response.value.outcome is Outcome.UNKNOWN
    assert original.claim is not None
    assert replay.claim is not None
    assert fresh.claim is not None
    assert original.claim.mode == "execute"
    assert original.claim.permitted_actions == ("seal_request",)
    assert original.claim.attempt_id == case.binding.attempt_id
    assert original.claim.limits["stall_after_us"] == "19"
    original_deadline = original.claim.deadline.value
    assert original_deadline is not None
    assert original_deadline.deadline().at == START + timedelta(hours=1)
    assert original.claim.execution_policy["namespace_revision"] == 2
    assert replay.claim.execution_policy["namespace_revision"] == 3
    policy = cast(JsonObject, replay.claim.execution_policy["values"])
    assert cast(JsonObject, policy["namespace"])["require_protected_execution"] is False
    assert (
        replay.claim.model_copy(
            update={"execution_policy": original.claim.execution_policy}
        )
        == original.claim
    )
    assert before_replay == after_replay
    assert len(before_replay["attempt_dispatcher"]) == 1
    assert len(after_fresh["attempt_dispatcher"]) == 2
    (original_basis,) = before_replay["attempt_first_claim_basis"]
    assert original_basis["dispatcher_id"] == original.claim.dispatcher_id
    assert original_basis["configured_stall_us"] == "19"
    assert fresh.claim.attempt_id == tail.binding.attempt_id
    assert fresh.claim.dispatcher_id != original.claim.dispatcher_id
    assert fresh.claim.limits["stall_after_us"] == "4000023"
    assert {
        row["dispatcher_id"]: row["configured_stall_us"]
        for row in after_fresh["attempt_first_claim_basis"]
    } == {original.claim.dispatcher_id: "19", fresh.claim.dispatcher_id: "4000023"}


@pytest.mark.asyncio
async def test_predecessor_claim_route_keeps_legacy_replay_after_upgrade(
    empty_database_settings: DatabaseSettings,
    evaluator: Evaluator,
) -> None:
    profile = installed_worker_profile("2000")
    assert profile is not None
    limits = profile.limits
    database = Database(empty_database_settings)
    try:
        async with database.migration_connection() as migration:
            await migration.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0032")
            )
        case = await claim_case(database, evaluator)
        await authorize_claim_worker(database, case)
        clock = AuthorityClock()
        with captured("api", LoggingSettings(), "legacy-claim", clock.now) as capture:
            original_app = claim_application(database, capture, clock, limits=limits)

            async with (
                lifespan(original_app),
                KoraClient("http://127.0.0.1", BEARER, asgi_app=original_app) as client,
            ):
                original = await client.claim_worker(
                    case.query.worker_id, request(case)
                )
                replay_on_predecessor = await client.claim_worker(
                    case.query.worker_id, request(case)
                )
        async with database.read_transaction() as reader:
            legacy_receipts = tuple(
                await reader.scalars(select(attempt_dispatcher.c.id))
            )
    finally:
        await database.close()
    upgrade = await upgrade_database(empty_database_settings)
    database = Database(empty_database_settings)
    try:
        clock.moment += timedelta(seconds=20)
        before_replay = await state(database)
        with captured("api", LoggingSettings(), "legacy-claim", clock.now) as capture:
            replacement = claim_application(
                database,
                capture,
                clock,
                default_stall=timedelta(microseconds=29),
                limits=limits,
            )
            async with (
                lifespan(replacement),
                KoraClient("http://127.0.0.1", BEARER, asgi_app=replacement) as client,
            ):
                replay_after_migration = await client.claim_worker(
                    case.query.worker_id, request(case)
                )
        after = await state(database)
    finally:
        await database.close()

    assert (upgrade.from_revision, upgrade.to_revision, upgrade.steps) == (
        "0032",
        "0033",
        1,
    )
    assert original.claim is not None
    assert replay_after_migration.claim is not None
    assert original.claim.limits["stall_after_us"] == "17"
    assert replay_on_predecessor == original
    assert replay_after_migration.claim.limits["stall_after_us"] == "29"
    assert (
        replay_after_migration.claim.model_copy(
            update={"limits": original.claim.limits}
        )
        == original.claim
    )
    assert legacy_receipts == (original.claim.dispatcher_id,)
    assert [row["id"] for row in after["attempt_dispatcher"]] == list(legacy_receipts)
    assert after["attempt_first_claim_basis"] == []
    assert before_replay == after
