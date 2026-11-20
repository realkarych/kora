import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.util.concurrency import await_

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import prepare_claim_sources
from kora.db.claim_sources_v1 import SOURCE_FIELDS
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt, node_instance, run
from kora.engine.attempt_inputs import prepare_attempt_inputs
from kora.engine.claim_context import ClaimQuery
from kora.engine.claim_sources import insert_claim_attempt
from kora.engine.locks import TransitionLocks
from kora.engine.worker_registration import register_worker
from kora.ir.json import JsonObject
from kora.protocols.authentication import Principal
from tests.api.claim_support import admit_expiry_run, poller
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.db.closing import narrow_database
from tests.db.seed import START, Seeder
from tests.engine.claim_support import SOURCE_LIMITS, ClaimCase, claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.engine.test_claim_sources import ALLOWANCE, source_values
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


def expiry_ir(*, enclosing: bool) -> JsonObject:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    nodes = cast(JsonObject, graph["nodes"])
    if enclosing:
        cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    else:
        cast(JsonObject, nodes["agent"])["limits"] = {"timeout": "1h"}
        cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "2h"}
    return ir


async def add_sibling(database: Database, case: ClaimCase) -> str:
    run_id = case.binding.run.run_id
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(run_id)
        seed = Seeder(writer)
        node_id = await seed.node(run_id, "gate", status="queued")
        attempt_id = seed.new_id()
        original = (
            (
                await writer.execute(
                    select(attempt).where(attempt.c.id == case.binding.attempt_id)
                )
            )
            .mappings()
            .one()
        )
        values = {key: original[key] for key in original if key not in SOURCE_FIELDS}
        values.update(id=attempt_id, node_instance_id=node_id)
        values.pop("acknowledged_segment_offset")
        binding = replace(
            case.binding,
            node_id=node_id,
            attempt_id=attempt_id,
            node_path="gate",
        )
        sources = source_values()
        sources["input_bindings"] = prepare_attempt_inputs({}, {}).storage
        await insert_claim_attempt(
            writer,
            locks,
            binding,
            values,
            prepare_claim_sources(sources, {name: ALLOWANCE for name in sources}),
            SOURCE_LIMITS,
            START,
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(current_attempt_id=attempt_id)
        )
    return attempt_id


async def contender(
    database: Database, case: ClaimCase
) -> tuple[ClaimQuery, Principal]:
    identity = "contending-worker"
    domain = "contending-domain"
    async with database.transaction() as writer:
        seed = Seeder(writer)
        token_id = await seed.token("e" * 64, kind="worker", owner=identity)
        principal = case.principal.model_copy(
            update={"owner": identity, "token_id": token_id}
        )
        await register_worker(
            writer,
            replace(
                case.registration,
                name=identity,
                execution_domain_id=domain,
                process=case.registration.process.model_copy(update={"pid": 321}),
            ),
            principal,
            portable=False,
            now=START,
            event_id=seed.new_id(),
        )
    return (
        case.query.model_copy(
            update={
                "worker_id": identity,
                "execution_domain_id": domain,
                "claim_id": "contender",
            }
        ),
        principal,
    )


async def hold_target(database: Database, case: ClaimCase) -> None:
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == case.binding.run.run_id)
            .values(
                needs_scheduling=False,
                target_key="held",
                target_held_since=START,
            )
        )


@pytest.mark.asyncio
async def test_claim_attempt_expiry_survives_rollback_before_worker_commit(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=False))
    await admit_expiry_run(database, case, "2h")
    first = await case.claim(database)
    assert first.delivery is not None
    await hold_target(database, case)
    cutoff = START + timedelta(hours=1)
    clock = AuthorityClock(cutoff)
    observed = False

    def roll_back(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal observed
        if statement.startswith("UPDATE worker SET") and "last_seen_at=" in statement:
            clock.moment = cutoff - timedelta(seconds=1)
            observed = True

    engine = database.writer_engine.sync_engine
    event.listen(engine, "after_cursor_execute", roll_back)
    poll = poller(database, clock)
    fresh = case.query.model_copy(update={"claim_id": "expiry"})

    try:
        expired = await poll.poll(fresh, case.principal)
        after = await state(database)
    finally:
        event.remove(engine, "after_cursor_execute", roll_back)
    recovered = await poll.poll(fresh, case.principal)

    assert observed
    assert expired is None
    assert after["attempt"][0]["deadline_expired_at"] == cutoff
    assert after["attempt"][0]["desired_state"] == "cancel"
    assert after["attempt"][0]["cancel_cause"] == "timeout"
    assert after["attempt"][0]["recovery_required"] is True
    assert after["attempt"][0]["lease_epoch"] == first.delivery.system.lease_epoch
    assert after["run"][0]["deadline_expired_at"] is None
    assert after["run"][0]["needs_scheduling"] is True
    assert after["run"][0]["target_held_since"] == START
    assert recovered is not None
    assert recovered.system.mode == "recover"
    assert database.units_in_flight() == 0


@dataclass
class HeldExpiryContinuation:
    database: Database
    clock: AuthorityClock
    rolled_back: datetime
    sqlite_transactions: int = 0
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def wait_before_lock(self) -> None:
        self.clock.moment = self.rolled_back
        self.entered.set()
        await_(self.release.wait())

    def before_checkout(
        self, connection: object, record: object, proxy: object
    ) -> None:
        self.sqlite_transactions += 1
        if self.sqlite_transactions == 2:
            self.wait_before_lock()

    def before_statement(
        self, connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("SELECT pg_advisory_xact_lock("):
            self.wait_before_lock()

    @contextmanager
    def installed(self) -> Generator[None]:
        engine = self.database.writer_engine.sync_engine
        if self.database.dialect is Dialect.SQLITE:
            name, listener = "checkout", self.before_checkout
        else:
            name, listener = "before_cursor_execute", self.before_statement
        event.listen(engine, name, listener)
        try:
            yield
        finally:
            event.remove(engine, name, listener)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_run_latch_denies_competing_claim_and_live_heartbeat_before_cascade(
    database: Database, evaluator: Evaluator, interrupted: bool
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=True))
    await admit_expiry_run(database, case, "1s")
    sibling = await add_sibling(database, case)
    clock = AuthorityClock()
    first_poll = poller(database, clock)
    delivery = await first_poll.poll(case.query, case.principal)
    assert delivery is not None
    query, principal = await contender(database, case)
    await hold_target(database, case)
    other = narrow_database(database)
    cutoff = START + timedelta(seconds=1)
    clock.moment = cutoff
    hold = HeldExpiryContinuation(database, clock, START)
    concurrent_poll = poller(other, clock)
    fresh = case.query.model_copy(update={"claim_id": "expiry"})

    try:
        with hold.installed():
            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                original = tasks.create_task(first_poll.poll(fresh, case.principal))
                try:
                    await hold.entered.wait()
                    pending = await state(other)
                    replayed = await concurrent_poll.poll(case.query, case.principal)
                    after_replay = await state(other)
                    beat = await heartbeat(
                        other, case, clock, heartbeat_body(delivery.system)
                    )
                    rival = await concurrent_poll.poll(query, principal)
                    if interrupted:
                        original.cancel()
                finally:
                    hold.release.set()
                if interrupted:
                    with pytest.raises(asyncio.CancelledError):
                        await original
                    result = None
                else:
                    result = await original
        final = await state(database)
    finally:
        await other.close()

    assert result is None
    assert rival is None
    assert replayed is not None
    assert replayed.encode() == delivery.encode()
    assert after_replay == pending
    assert pending["run"][0]["deadline_expired_at"] == cutoff
    assert pending["run"][0]["needs_scheduling"] is True
    assert {row["desired_state"] for row in pending["attempt"]} == {"run"}
    assert beat.mode == "recover"
    assert beat.desired_state == "cancel"
    assert beat.permitted_actions == ("observe", "retire")
    assert {row["id"] for row in final["attempt"]} == {case.binding.attempt_id, sibling}
    assert {row["cancel_cause"] for row in final["attempt"]} == {"timeout"}
    assert {row["updated_at"] for row in final["attempt"]} == {cutoff}
    assert [row["kind"] for row in final["event"]].count(
        "attempt.recovery_required"
    ) == 1
    assert len(final["attempt_dispatcher"]) == 1
    assert final["run"][0]["target_held_since"] == START
    assert final["run"][0]["status"] == "running"
    assert database.units_in_flight() == other.units_in_flight() == 0
