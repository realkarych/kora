import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest
from sqlalchemy import Connection, delete, event, insert, select, text, update
from sqlalchemy.util import await_

from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    attempt_claim_read_admission,
    attempt_dispatcher,
    run,
)
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.claim_timing import first_claim_deadline
from kora.engine.locks import TransitionLocks
from kora.engine.run_promotion import PromotionOutcome
from kora.ir.json import JsonObject
from kora.models.errors import ApiErrorCode
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from kora.models.worker_claims import WorkerClaimRequest
from tests.api.deployment_cases import GRAPH
from tests.api.scheduler_claim_support import (
    PARAMS,
    SchedulerClaimSetup,
    node_id,
    scheduler_claim_ir,
    service_identity,
)
from tests.api.support import client_for
from tests.api.worker_support import registration
from tests.db.closing import ConnectionLedger
from tests.engine.admission_processing_support import process
from tests.engine.claim_support import read_claim_state, state
from tests.engine.promotion_support import PromotionCase
from tests.engine.test_http_trigger_admission import RUNS, VERSIONS, promotion_from_http

MAX_UTC: datetime = datetime.max.replace(tzinfo=UTC)
COMPETITOR: str = "separate-scheduler-handoff-worker"
COMPETING_CREDENTIAL: str = "scheduler-handoff-competing-fixture-credential"


@dataclass(frozen=True)
class Candidate:
    promotion: PromotionCase
    deployed: DeployResponse
    node_id: str
    promoted_at: datetime

    @property
    def run_id(self) -> str:
        return self.promotion.run_id


async def promote_candidate(
    setup: SchedulerClaimSetup,
    key: str,
    *,
    attempt_timeout: str | None = None,
) -> Candidate:
    ir = scheduler_claim_ir()
    if attempt_timeout is not None:
        graph = cast(JsonObject, ir["graph"])
        agent = cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])
        agent["limits"] = {"timeout": attempt_timeout}
        cast(JsonObject, ir["runtime"]).pop("limits")
    headers = {"Authorization": str(setup.user.request.authorization)}
    async with client_for(setup.app) as client:
        deployment = await client.post(
            VERSIONS, json={"graph": GRAPH, "ir": ir, "resources": []}, headers=headers
        )
        accepted = await client.post(
            RUNS, json={"params": PARAMS}, headers=headers | {"Idempotency-Key": key}
        )
    assert deployment.status_code == accepted.status_code == 201
    database = setup.processor.database
    deployed = DeployResponse.model_validate_json(deployment.content)
    promoted, _ = await promotion_from_http(
        database,
        setup.user.principal.namespaces[0].namespace_id,
        AcceptedRun.model_validate_json(accepted.content),
        deployed,
        setup.clock,
        setup.processor.identifiers,
        key=key,
    )
    if attempt_timeout is None:
        setup.clock.advance(1)
    promotion_time = setup.clock.now()
    assert await promoted.promote(database) is PromotionOutcome.APPLIED
    return Candidate(
        promoted,
        deployed,
        await node_id(database, promoted.run_id, "agent"),
        promotion_time,
    )


async def reserve_crossing_candidate(setup: SchedulerClaimSetup) -> Candidate:
    now = setup.clock.now()
    millis = (MAX_UTC - now) // timedelta(milliseconds=1) - 1000
    target = await promote_candidate(
        setup, "crossing-cutoff", attempt_timeout=f"{millis}ms"
    )
    assert (
        first_claim_deadline(
            target.run_id,
            target.node_id,
            now,
            now,
            None,
            millis * 1000,
            timedelta(0),
        )
        is not None
    )
    assert await process(setup.processor, target.node_id, periodic=False) is (
        AttemptAdmissionState.QUEUED
    )
    return target


async def clear_scheduling_mark(database: Database, target: Candidate) -> None:
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        assert await locks.acquire_run(target.run_id)
        await writer.execute(
            update(run).where(run.c.id == target.run_id).values(needs_scheduling=False)
        )


async def refuse_claim(
    client: KoraClient, name: str, request: WorkerClaimRequest
) -> ApiError:
    with pytest.raises(ApiError) as refused:
        await client.claim_worker(name, request)
    assert refused.value.correlation.request_id is not None
    return refused.value


def assert_original_discharge(
    before: dict[str, list[dict[str, object]]],
    after: dict[str, list[dict[str, object]]],
    target: Candidate,
    now: datetime,
) -> None:
    (original,) = before["attempt"]
    (retired,) = after["attempt"]
    assert original["status"] == "created"
    assert retired == original | {
        "status": "cancelled",
        "desired_state": "cancel",
        "finished_at": now,
        "updated_at": now,
    }
    assert retired["lease_epoch"] == 0
    assert retired["execution_state"] == "not_started"
    assert all(
        retired[key] is None
        for key in (
            "worker_id",
            "execution_domain_id",
            "lease_owner",
            "lease_until",
            "handle",
            "started_at",
            "deadline",
            "deadline_expired_at",
        )
    )
    assert after["attempt_dispatcher"] == after["attempt_first_claim_basis"] == []
    assert before["node_instance"] == after["node_instance"]
    assert before["worker"] == after["worker"]
    owner = next(row for row in before["run"] if row["id"] == target.run_id)
    assert owner["needs_scheduling"] is False
    assert after["run"] == [
        row | {"needs_scheduling": True, "updated_at": now} if row == owner else row
        for row in before["run"]
    ]
    (event_row,) = [row for row in after["event"] if row not in before["event"]]
    assert (event_row["kind"], event_row["run_id"], event_row["attempt_id"]) == (
        "attempt.cancelled",
        target.run_id,
        original["id"],
    )


async def verify_nonterminal_refusals(
    database: Database, setup: SchedulerClaimSetup, client: KoraClient, worker_id: str
) -> None:
    engine = database.writer_engine.sync_engine
    entry_rolled_back = False

    def restore_clock_after_first_writer(_connection: Connection) -> None:
        nonlocal entry_rolled_back
        if not entry_rolled_back:
            entry_rolled_back = True
            setup.clock.advance(-2)

    original = await state(database)
    (saved,) = original["attempt"]
    wrong_domain = setup.request.model_copy(
        update={"execution_domain_id": "domain-" + "c" * 64}
    )

    forbidden = await refuse_claim(client, worker_id, wrong_domain)
    after_forbidden = await state(database)
    event.listen(engine, "rollback", restore_clock_after_first_writer)
    try:
        stale = await refuse_claim(client, worker_id, setup.request)
    finally:
        event.remove(engine, "rollback", restore_clock_after_first_writer)
    after_stale = await state(database)
    setup.clock.advance(2)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == saved["id"])
            .values(current_dispatcher_open=True)
        )
    unsafe_before = await state(database)
    unsafe = await refuse_claim(client, worker_id, setup.request)
    unsafe_after = await state(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == saved["id"])
            .values(current_dispatcher_open=None, desired_state="cancel")
        )
    cancelling_before = await state(database)
    cancelling = await client.claim_worker(worker_id, setup.request)
    cancelling_after = await state(database)

    assert (forbidden.status, forbidden.code) == (403, ApiErrorCode.FORBIDDEN)
    assert after_forbidden == original
    assert entry_rolled_back
    assert (stale.status, stale.code) == (500, ApiErrorCode.INTERNAL)
    assert after_stale == original
    assert (unsafe.status, unsafe.code) == (500, ApiErrorCode.INTERNAL)
    assert unsafe_after == unsafe_before
    assert cancelling.claim is None
    for table in (
        "attempt",
        "attempt_dispatcher",
        "attempt_first_claim_basis",
        "event",
    ):
        assert cancelling_after[table] == cancelling_before[table]

    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == saved["id"])
            .values(desired_state="run")
        )

    empty_page = await client.claim_worker(worker_id, setup.request)
    assert empty_page.claim is None


async def verify_saved_fact_changed_before_retry(
    setup: SchedulerClaimSetup,
    target: Candidate,
    client: KoraClient,
    worker_id: str,
    fact: Literal["source", "dispatch", "lease"],
) -> None:
    database = setup.processor.database
    engine = database.writer_engine.sync_engine
    rolled_back = False
    retry_entered = asyncio.Event()
    allow_retry = asyncio.Event()
    (saved,) = (await state(database))["attempt"]
    attempt_id = saved["id"]
    now = setup.clock.now()
    dispatcher_id = setup.processor.identifiers.new()
    native_source = attempt_claim_read_admission
    source = native_source.c.attempt_id == attempt_id
    revision = native_source.c.source_revision
    physical = attempt.c.id == attempt_id

    def observe_rollback(_connection: Connection) -> None:
        nonlocal rolled_back
        rolled_back = True

    def hold_retry_checkout(*arguments: object) -> None:
        if rolled_back and not retry_entered.is_set():
            retry_entered.set()
            await_(allow_retry.wait())

    other_owner = Database(database.settings)
    event.listen(engine, "rollback", observe_rollback)
    event.listen(engine, "checkout", hold_retry_checkout)
    try:
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            request = tasks.create_task(refuse_claim(client, worker_id, setup.request))
            try:
                await retry_entered.wait()
                async with other_owner.transaction() as writer:
                    locks = TransitionLocks(writer)
                    assert await locks.acquire_run(target.run_id, wait=False)
                    if fact == "source":
                        await writer.execute(
                            update(native_source)
                            .where(source)
                            .values(source_revision=revision + 1)
                        )
                    elif fact == "dispatch":
                        await writer.execute(
                            insert(attempt_dispatcher).values(
                                id=dispatcher_id,
                                attempt_id=attempt_id,
                                execution_domain_id=setup.request.execution_domain_id,
                                worker_id=worker_id,
                                incarnation_id=setup.request.incarnation_id,
                                lease_epoch=1,
                                mode="execute",
                                granted_at=now,
                                closed_at=now,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        await writer.execute(
                            update(attempt)
                            .where(physical)
                            .values(
                                lease_owner=worker_id,
                                lease_epoch=1,
                                lease_until=now + timedelta(seconds=120),
                            )
                        )
                expected = await state(database)
            finally:
                allow_retry.set()
            refused = await request
        unchanged = await state(database)

        assert (refused.status, refused.code) == (500, ApiErrorCode.INTERNAL)
        assert unchanged == expected
        assert unchanged["attempt"][0]["status"] == "created"
    finally:
        event.remove(engine, "checkout", hold_retry_checkout)
        event.remove(engine, "rollback", observe_rollback)
        await other_owner.close()

    async with database.transaction() as writer:
        if fact == "source":
            await writer.execute(
                update(native_source).where(source).values(source_revision=revision - 1)
            )
        elif fact == "dispatch":
            await writer.execute(
                delete(attempt_dispatcher).where(
                    attempt_dispatcher.c.id == dispatcher_id
                )
            )
        else:
            await writer.execute(
                update(attempt)
                .where(physical)
                .values(lease_owner=None, lease_epoch=0, lease_until=None)
            )


async def verify_repeated_refusal_cannot_release_again(
    origin: str,
    setup: SchedulerClaimSetup,
    client: KoraClient,
    worker_id: str,
    target: Candidate,
) -> None:
    database = setup.processor.database
    competitor = await service_identity(
        database,
        setup.user.principal.namespaces[0].namespace_id,
        credential=COMPETING_CREDENTIAL,
        owner=COMPETITOR,
        seeder=setup.seeder,
    )
    competing_registration = registration(competitor)
    other = competing_registration.model_copy(
        update={
            "incarnation_id": "incarnation-" + "b" * 64,
            "execution_domain_id": "domain-" + "b" * 64,
            "process": competing_registration.process.model_copy(update={"pid": 4321}),
        }
    )
    competing_request = setup.request.model_copy(
        update={
            "incarnation_id": other.incarnation_id,
            "execution_domain_id": other.execution_domain_id,
        }
    )

    competitor_selected = asyncio.Event()
    allow_competitor = asyncio.Event()
    reader = database.reader_engine.sync_engine
    writer = database.writer_engine.sync_engine
    probe = HeldDischargeCommit(commit=True)

    def hold_competing_selection(
        _connection: object, _cursor: object, statement: str, *arguments: object
    ) -> None:
        if (
            probe.entered.is_set()
            and "ORDER BY runtime, id" in statement
            and not competitor_selected.is_set()
        ):
            competitor_selected.set()
            await_(allow_competitor.wait())

    async with KoraClient(origin, COMPETING_CREDENTIAL) as competing:
        other_worker = (await competing.register_worker(other)).worker_id
        before = await state(database)
        event.listen(writer, "after_cursor_execute", probe.record_write)
        event.listen(writer, "commit", probe.hold_commit)
        event.listen(reader, "after_cursor_execute", hold_competing_selection)
        try:
            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                original = tasks.create_task(
                    refuse_claim(client, worker_id, setup.request)
                )
                try:
                    await probe.entered.wait()
                    contender = tasks.create_task(
                        competing.claim_worker(other_worker, competing_request)
                    )
                    await competitor_selected.wait()
                    while_held = await state(database)
                    probe.release.set()
                    refused = await original
                    discharged = await state(database)
                finally:
                    probe.release.set()
                    allow_competitor.set()
            competed = contender.result()
        finally:
            event.remove(reader, "after_cursor_execute", hold_competing_selection)
            event.remove(writer, "commit", probe.hold_commit)
            event.remove(writer, "after_cursor_execute", probe.record_write)
        repeated = await client.claim_worker(worker_id, setup.request)
    after_repeats = await state(database)

    assert (refused.status, refused.code) == (500, ApiErrorCode.INTERNAL)
    assert target.run_id not in refused.content.decode()
    assert while_held == before
    assert_original_discharge(before, discharged, target, setup.clock.now())
    assert repeated.claim is competed.claim is None
    for table in (
        "attempt",
        "attempt_dispatcher",
        "attempt_first_claim_basis",
        "event",
    ):
        assert discharged[table] == after_repeats[table]


class HeldDischargeCommit:
    def __init__(self, commit: bool) -> None:
        self.commit = commit
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.connection: object | None = None

    def record_write(
        self, connection: object, _cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("UPDATE attempt SET") and "finished_at=" in statement:
            self.connection = connection

    def hold_commit(self, connection: Connection) -> None:
        if connection is self.connection and not self.entered.is_set():
            self.entered.set()
            await_(self.release.wait())
            if not self.commit:
                connection.exec_driver_sql(
                    "INSERT INTO first_claim_commit_probe (id, parent_id) VALUES (1, 1)"
                )


async def verify_original_commit_settlement(
    setup: SchedulerClaimSetup,
    target: Candidate,
    client: KoraClient,
    worker_id: str,
    ledger: ConnectionLedger,
    *,
    commit: bool,
) -> None:
    database = setup.processor.database
    engine = database.writer_engine.sync_engine
    probe = HeldDischargeCommit(commit)
    refusal: ApiError | None = None
    if not commit:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "CREATE TABLE first_claim_commit_parent (id INTEGER PRIMARY KEY)"
            )
            await writer.exec_driver_sql(
                "CREATE TABLE first_claim_commit_probe (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER NOT NULL REFERENCES first_claim_commit_parent (id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )

    before = await state(database)
    (original,) = before["attempt"]
    original_status = select(attempt.c.status).where(attempt.c.id == original["id"])
    event.listen(engine, "after_cursor_execute", probe.record_write)
    event.listen(engine, "commit", probe.hold_commit)
    try:
        async with AsyncExitStack() as snapshot:
            async with asyncio.timeout(10):
                request = asyncio.create_task(
                    refuse_claim(client, worker_id, setup.request)
                )
                try:
                    await probe.entered.wait()
                    if commit:
                        request.cancel("caller left at original commit")
                        cancellation_turn = asyncio.Event()
                        asyncio.get_running_loop().call_soon(cancellation_turn.set)
                        await cancellation_turn.wait()
                    original_checkouts = ledger.outstanding
                    reader = await snapshot.enter_async_context(
                        database.read_snapshot()
                    )
                    status_during_commit = (
                        await reader.execute(original_status)
                    ).scalar_one()
                    awaiting_original_commit = not request.done()
                    held_checkouts = ledger.outstanding
                finally:
                    probe.release.set()
                    if commit:
                        with pytest.raises(asyncio.CancelledError):
                            await request
                    else:
                        refusal = await request
            observer_checkouts = ledger.outstanding
            during_commit = await read_claim_state(reader)
    finally:
        event.remove(engine, "commit", probe.hold_commit)
        event.remove(engine, "after_cursor_execute", probe.record_write)
    after = await state(database)

    assert awaiting_original_commit
    assert status_during_commit == "created"
    assert during_commit == before
    assert (original_checkouts, held_checkouts, observer_checkouts) == (1, 2, 1)
    if commit:
        assert_original_discharge(before, after, target, setup.clock.now())
    else:
        assert refusal is not None
        assert (refusal.status, refusal.code) == (500, ApiErrorCode.INTERNAL)
        assert "first_claim_commit_probe" not in refusal.content.decode()
        assert after == before
        async with database.transaction() as writer:
            assert (
                await writer.scalar(
                    text("SELECT count(*) FROM first_claim_commit_probe")
                )
                == 0
            )
    assert ledger.outstanding == database.units_in_flight() == 0


async def admit_after_first_claim_deadline_crossing(
    origin: str,
    setup: SchedulerClaimSetup,
    client: KoraClient,
    worker_id: str,
    ledger: ConnectionLedger,
    *,
    transport: str,
) -> Candidate:
    database = setup.processor.database
    original = await reserve_crossing_candidate(setup)
    safe = await promote_candidate(setup, "representable-cutoff")
    waiting = await process(setup.processor, safe.node_id, periodic=False)
    await clear_scheduling_mark(database, original)
    setup.clock.advance(1)

    if transport == "http":
        await verify_nonterminal_refusals(database, setup, client, worker_id)
        for fact in ("source", "dispatch", "lease"):
            await verify_saved_fact_changed_before_retry(
                setup, original, client, worker_id, fact
            )
        await verify_original_commit_settlement(
            setup, original, client, worker_id, ledger, commit=False
        )
        await verify_repeated_refusal_cannot_release_again(
            origin, setup, client, worker_id, original
        )
    else:
        await verify_original_commit_settlement(
            setup, original, client, worker_id, ledger, commit=True
        )
    queued = await process(setup.processor, safe.node_id)

    assert waiting is AttemptAdmissionState.WAITING
    assert queued is AttemptAdmissionState.QUEUED
    return safe
