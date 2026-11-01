import asyncio
from dataclasses import replace
from typing import Literal

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseClosedError, Dialect
from kora.db.reads import namespace_outstanding_usage
from kora.db.tables import event, graph_version, run
from kora.db.transactions import WriterTransactionError
from kora.engine.cancellation import CancellationOutcome, CancellationResult
from kora.engine.locks import LockError, Scope, ScopeKind, TransitionLocks
from kora.protocols.authentication import Principal
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.cancellation_support import pending_cancellation
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [False, True])
async def test_cancel_pending_run_is_atomic(database: Database, marked: bool) -> None:
    scenario = await pending_cancellation(database, marked)
    before, _, receipt = await scenario.snapshot()

    result = await scenario.apply()
    after, events, saved_receipt = await scenario.snapshot()

    assert result == CancellationResult(CancellationOutcome.APPLIED, not marked)
    assert after == before | {
        "status": "cancelled",
        "finished_at": scenario.clock.moment,
        "updated_at": scenario.clock.moment,
        "needs_scheduling": True,
    }
    assert saved_receipt == receipt
    assert len(events) == 1
    assert events[0] == {
        "id": events[0]["id"],
        "namespace_id": scenario.namespace_id,
        "run_id": scenario.run_id,
        "node_instance_id": None,
        "attempt_id": None,
        "kind": "run.cancelled",
        "actor": {
            "kind": "user",
            "owner": scenario.principal.owner,
            "token_id": scenario.principal.token_id,
        },
        "payload": {
            "trigger": before["trigger"],
            "graph_version": scenario.version_id,
            "rerun_of": scenario.original_id,
        },
        "at": scenario.clock.moment,
        "created_at": scenario.clock.moment,
        "updated_at": scenario.clock.moment,
    }
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["rollback", "event_failure"])
async def test_cancel_pending_run_rolls_back_every_effect(
    database: Database, ending: str
) -> None:
    scenario = await pending_cancellation(database)
    if ending == "event_failure":
        async with database.transaction() as connection:
            await connection.execute(
                insert(event).values(
                    id=scenario.identifiers().new(),
                    namespace_id=scenario.namespace_id,
                    run_id=scenario.run_id,
                    node_instance_id=None,
                    attempt_id=None,
                    kind="existing",
                    actor={"kind": "system"},
                    payload={},
                    at=scenario.clock.moment,
                    created_at=scenario.clock.moment,
                    updated_at=scenario.clock.moment,
                )
            )
    before = await scenario.snapshot()
    expected_error = DeliberateRollback if ending == "rollback" else IntegrityError

    async def failing_transaction() -> None:
        async with database.transaction() as connection:
            await scenario.transition(connection, await scenario.acquire(connection))
            raise DeliberateRollback()

    with pytest.raises(expected_error):
        await failing_transaction()
    after = await scenario.snapshot()

    assert after == before
    async with database.read_transaction() as connection:
        assert await namespace_outstanding_usage(connection, scenario.namespace_id) == 1
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_cancel_pending_run_replay_is_unchanged(database: Database) -> None:
    scenario = await pending_cancellation(database)
    await scenario.apply()
    before = await scenario.snapshot()
    reads = scenario.clock.reads
    other = Principal.model_validate(
        scenario.principal.model_dump()
        | {"owner": scenario.original_id, "token_id": scenario.run_id}
    )
    scenario.clock.advance(60)

    result = await scenario.apply(other)
    after = await scenario.snapshot()

    assert result == CancellationResult(CancellationOutcome.UNCHANGED)
    assert before == after
    assert scenario.clock.reads == reads


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize(
    "authority", ["service", "no_grant", "foreign_run", "wrong_graph", "missing"]
)
async def test_cancel_pending_run_checks_authority_and_scope(
    database: Database, cancelled: bool, authority: str
) -> None:
    scenario = await pending_cancellation(database)
    if cancelled:
        await scenario.apply()
    before = await scenario.snapshot()
    changes: dict[str, object] = {}
    if authority == "service":
        changes = {"kind": "service", "runtime": "local"}
    elif authority == "no_grant":
        changes = {"namespaces": ()}
    elif authority == "foreign_run":
        async with database.transaction() as connection:
            seed = Seeder(connection)
            foreign_namespace = await seed.namespace("foreign")
            foreign_graph = await seed.graph(foreign_namespace)
            foreign_version = await seed.graph_version(foreign_graph)
            foreign_run = await seed.run(
                foreign_namespace, foreign_graph, foreign_version, "pending"
            )
        scenario = replace(scenario, run_id=foreign_run)
    elif authority == "wrong_graph":
        scenario = replace(scenario, graph_id=scenario.original_id)
    else:
        scenario = replace(scenario, run_id=scenario.principal.token_id)
    principal = Principal.model_validate(scenario.principal.model_dump() | changes)
    reads = scenario.clock.reads

    result = await scenario.apply(principal)

    assert result == CancellationResult(CancellationOutcome.REFUSED)
    assert scenario.clock.reads == reads
    original = replace(
        scenario, run_id=str(before[0]["id"]), graph_id=str(before[0]["graph_id"])
    )
    assert await original.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [
        "running",
        "failed",
        "succeeded",
        "cancelled",
        "node",
        "hold",
        "started",
        "expired",
        "finished",
        "wrong_version",
        "wrong_namespace",
    ],
)
async def test_cancel_pending_run_refuses_materialized_or_active_state(
    database: Database, shape: str
) -> None:
    scenario = await pending_cancellation(database)
    async with database.transaction() as connection:
        changes: dict[str, object] = {}
        if shape == "node":
            await Seeder(connection).node(scenario.run_id, "retained-node", "cancelled")
        elif shape == "hold":
            changes = {"target_held_since": scenario.clock.moment}
        elif shape == "started":
            changes = {"started_at": scenario.clock.moment}
        elif shape == "expired":
            changes = {"deadline_expired_at": scenario.clock.moment}
        elif shape == "finished":
            changes = {"finished_at": scenario.clock.moment}
        elif shape in {"wrong_version", "wrong_namespace"}:
            seed = Seeder(connection)
            other_namespace = await seed.namespace("other")
            other_graph = await seed.graph(other_namespace)
            other_version = await seed.graph_version(other_graph)
            changes = (
                {"graph_version_id": other_version}
                if shape == "wrong_version"
                else {"namespace_id": other_namespace}
            )
        else:
            changes = {"status": shape}
        if changes:
            await connection.execute(
                update(run).where(run.c.id == scenario.run_id).values(**changes)
            )
    before = await scenario.snapshot()

    result = await scenario.apply()

    assert result == CancellationResult(CancellationOutcome.REFUSED)
    assert await scenario.snapshot() == before
    assert scenario.clock.reads == 0


@pytest.mark.asyncio
async def test_cancel_pending_run_does_not_require_current_deployment(
    database: Database,
) -> None:
    scenario = await pending_cancellation(database)
    async with database.transaction() as connection:
        await connection.execute(
            update(graph_version)
            .where(graph_version.c.id == scenario.version_id)
            .values(status="retired", execution_contract={})
        )
        await connection.execute(
            update(run)
            .where(run.c.id == scenario.run_id)
            .values(retention_version=None, rerun_of_run_id=None)
        )

    result = await scenario.apply()
    after, events, _ = await scenario.snapshot()

    assert result.outcome is CancellationOutcome.APPLIED
    assert after["retention_version"] is None
    assert events[0]["payload"] == {
        "trigger": after["trigger"],
        "graph_version": scenario.version_id,
        "rerun_of": None,
    }


@pytest.mark.asyncio
async def test_cancel_pending_run_releases_backlog(database: Database) -> None:
    scenario = await pending_cancellation(database)
    async with database.transaction() as connection:
        await Seeder(connection).run(
            scenario.namespace_id, scenario.graph_id, scenario.version_id, "pending"
        )
        before = await namespace_outstanding_usage(connection, scenario.namespace_id)

    first, second = await asyncio.gather(scenario.apply(), scenario.apply())
    async with database.read_transaction() as connection:
        after = await namespace_outstanding_usage(connection, scenario.namespace_id)
    _, events, _ = await scenario.snapshot()

    assert {first.outcome, second.outcome} == {
        CancellationOutcome.APPLIED,
        CancellationOutcome.UNCHANGED,
    }
    assert sum(result.wake_required for result in (first, second)) == 1
    assert (before, after) == (2, 1)
    assert len(events) == 1
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("promotion", [False, True])
async def test_cancel_pending_run_rechecks_after_lock_wait(
    database: Database, promotion: bool
) -> None:
    scenario = await pending_cancellation(database)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with database.transaction() as connection:
            locks = await scenario.acquire(connection)
            if promotion:
                await connection.execute(
                    update(run)
                    .where(run.c.id == scenario.run_id)
                    .values(status="running", started_at=scenario.clock.moment)
                )
                await Seeder(connection).node(
                    scenario.run_id, "promoted-node", "pending"
                )
            else:
                await scenario.transition(connection, locks)
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    owner = asyncio.create_task(holder())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower = asyncio.create_task(scenario.apply())
    try:
        await wait_for_lock_waiter(database)
        blocked = not follower.done()
    finally:
        release.set()
        await asyncio.wait_for(owner, DEADLINE)
    result = await asyncio.wait_for(follower, DEADLINE)
    after, events, _ = await scenario.snapshot()

    assert blocked
    assert result.outcome is (
        CancellationOutcome.REFUSED if promotion else CancellationOutcome.UNCHANGED
    )
    assert after["status"] == ("running" if promotion else "cancelled")
    assert len(events) == (0 if promotion else 1)
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_cancel_pending_run_requires_exact_live_locks(database: Database) -> None:
    scenario = await pending_cancellation(database)
    before = await scenario.snapshot()

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(scenario.run_id)
        await locks.acquire_scopes((Scope(ScopeKind.GLOBAL),))
        with pytest.raises(LockError, match="not held"):
            await scenario.transition(connection, locks)
    async with database.transaction() as connection:
        locks = await scenario.acquire(connection)
        if database.dialect is Dialect.POSTGRESQL:
            async with database.transaction() as other:
                with pytest.raises(LockError, match="does not own"):
                    await scenario.transition(other, locks)
        else:
            async with database.read_transaction() as reader:
                with pytest.raises(LockError, match="does not own"):
                    await scenario.transition(reader, locks)
    async with database.transaction() as connection:
        with pytest.raises(WriterTransactionError, match="not current"):
            await scenario.transition(connection, locks)

    assert await scenario.snapshot() == before
    assert scenario.clock.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["request_cancel", "body_cancel", "shutdown"])
async def test_cancel_pending_run_transaction_lifetime(
    database: Database, ending: Literal["request_cancel", "body_cancel", "shutdown"]
) -> None:
    scenario = await pending_cancellation(database)
    ledger = ConnectionLedger(database)
    before = await scenario.snapshot()
    ready = asyncio.Event()
    release = asyncio.Event()

    async def body(connection: AsyncConnection) -> CancellationResult:
        result = await scenario.transition(
            connection, await scenario.acquire(connection)
        )
        ready.set()
        await asyncio.wait_for(release.wait(), DEADLINE)
        if ending == "body_cancel":
            raise asyncio.CancelledError()
        return result

    caller = asyncio.create_task(database.run(body))
    await asyncio.wait_for(ready.wait(), DEADLINE)
    if ending == "request_cancel":
        caller.cancel()
        barrier = asyncio.Event()
        asyncio.get_running_loop().call_soon(barrier.set)
        await asyncio.wait_for(barrier.wait(), DEADLINE)
        assert not caller.done()
    elif ending == "shutdown":
        await database.close(deadline_seconds=0)
    release.set()
    if ending == "shutdown":
        with pytest.raises(DatabaseClosedError):
            await asyncio.wait_for(caller, DEADLINE)
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, DEADLINE)
    reopened = Database(database.settings)
    try:
        after = await replace(scenario, database=reopened).snapshot()
    finally:
        await reopened.close()

    if ending == "request_cancel":
        assert after[0]["status"] == "cancelled"
        assert len(after[1]) == 1
    else:
        assert after == before
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
