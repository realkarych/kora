import asyncio
from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import insert, update

from kora.db.connection import Database, Lifecycle
from kora.db.tables import attempt, namespace, node_instance, run
from kora.db.transactions import WriterTransactionError
from kora.engine.cancellation import CancellationOutcome, cancel_pending_run
from kora.engine.locks import LockError, ScopeKind, TransitionLocks
from kora.engine.start_capacity import (
    CapacityOutcome,
    InvalidCapacityPolicy,
    Overlap,
    StartCapacity,
)
from kora.ir.json import JsonValue
from kora.protocols.authentication import Principal
from tests.db.closing import ConnectionLedger, until
from tests.db.seed import START, Seeder
from tests.engine.capacity_support import PRIVATE, capacity_scenario
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)
from tests.engine.test_start_replay import observed_statements


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap", ["allow", "queue", "skip"])
@pytest.mark.parametrize(
    "status", ["pending", "running", "succeeded", "failed", "cancelled"]
)
async def test_start_capacity_preserves_overlap_before_quota(
    database: Database, overlap: Overlap, status: str
) -> None:
    scenario = await capacity_scenario(database)
    await scenario.policy({"concurrency": {"max_pending_runs": 1}})
    async with database.transaction() as connection:
        seed = Seeder(connection)
        second_version = await seed.graph_version(scenario.graph_id, 2)
        await seed.run(scenario.namespace_id, scenario.graph_id, second_version, status)

    result = await scenario.read(overlap)

    active = status in {"pending", "running"}
    expected = (
        StartCapacity(CapacityOutcome.SKIPPED)
        if overlap == "skip" and active
        else StartCapacity(
            CapacityOutcome.FULL if active else CapacityOutcome.ROOM, 1, int(active)
        )
    )
    assert result == expected


@pytest.mark.asyncio
async def test_start_capacity_skip_does_not_consult_malformed_quota(
    database: Database,
) -> None:
    scenario = await capacity_scenario(database)
    await scenario.policy({"concurrency": PRIVATE})
    async with database.transaction() as connection:
        await Seeder(connection).run(
            scenario.namespace_id, scenario.graph_id, scenario.version_id, "pending"
        )
    before = await scenario.snapshot()

    async with database.transaction() as connection:
        locks = await scenario.acquire(connection)
        async with observed_statements(connection) as statements:
            result = await scenario.check(connection, locks, "skip")

    assert result == StartCapacity(CapacityOutcome.SKIPPED)
    assert len(statements) == 2
    assert "EXISTS" in statements[1]
    assert all("policy" not in statement for statement in statements)
    assert before == await scenario.snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"concurrency": {}},
        {"concurrency": {"max_running_nodes": 1}},
        {"concurrency": {"max_pending_runs": 7}},
    ],
)
async def test_start_capacity_uses_live_namespace_policy(
    database: Database, policy: JsonValue
) -> None:
    scenario = await capacity_scenario(database)
    await scenario.policy(policy)

    first = await scenario.read()
    await scenario.policy({"concurrency": {"max_pending_runs": 1}})
    second = await scenario.read()

    assert first == StartCapacity(
        CapacityOutcome.ROOM,
        7 if policy == {"concurrency": {"max_pending_runs": 7}} else 1000,
        0,
    )
    assert second == StartCapacity(CapacityOutcome.ROOM, 1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        [],
        PRIVATE,
        {"concurrency": None},
        {"concurrency": []},
        {"concurrency": {"max_pending_runs": None}},
        {"concurrency": {"max_pending_runs": True}},
        {"concurrency": {"max_pending_runs": 1.5}},
        {"concurrency": {"max_pending_runs": 0}},
        {"concurrency": {"max_pending_runs": -1}},
        {"concurrency": {"max_pending_runs": PRIVATE}},
    ],
)
async def test_start_capacity_rejects_malformed_policy(
    database: Database, policy: JsonValue
) -> None:
    scenario = await capacity_scenario(database)
    await scenario.policy(policy)
    before = await scenario.snapshot()

    with pytest.raises(InvalidCapacityPolicy) as caught:
        await scenario.read()

    assert str(caught.value) == "the namespace outstanding-run policy is invalid"
    assert caught.value.__cause__ is None
    assert before == await scenario.snapshot()
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "physical_status", ["created", "claimed", "submitted", "running", "finishing"]
)
async def test_start_capacity_includes_unresolved_history(
    database: Database, physical_status: str
) -> None:
    scenario = await capacity_scenario(database)
    async with database.transaction() as connection:
        seed = Seeder(connection)
        cancelled = await seed.run(
            scenario.namespace_id, scenario.graph_id, scenario.version_id, "cancelled"
        )
        node_id = await seed.node(cancelled, "old-node", "cancelled")
        old_attempt = await seed.attempt(node_id, physical_status, 1)
        current_attempt = await seed.attempt(node_id, "succeeded", 2)
        await connection.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(current_attempt_id=current_attempt)
        )
        extra_node = await seed.node(cancelled, "another-node", "succeeded")
        await seed.attempt(extra_node, physical_status)
        held = await seed.run(
            scenario.namespace_id,
            scenario.graph_id,
            scenario.version_id,
            "failed",
            target_key="retained",
            target_held_since=START,
        )
        foreign_namespace = await seed.namespace("foreign")
        foreign_graph = await seed.graph(foreign_namespace)
        foreign_version = await seed.graph_version(foreign_graph)
        await seed.run(foreign_namespace, foreign_graph, foreign_version, "pending")

    full = await scenario.read("skip")
    async with database.transaction() as connection:
        await connection.execute(
            update(attempt).where(attempt.c.id == old_attempt).values(status="failed")
        )
    still_full = await scenario.read()
    async with database.transaction() as connection:
        await connection.execute(
            update(attempt)
            .where(attempt.c.node_instance_id == extra_node)
            .values(status="failed")
        )
    room = await scenario.read()

    assert full == still_full == StartCapacity(CapacityOutcome.FULL, 2, 2)
    assert room == StartCapacity(CapacityOutcome.ROOM, 2, 1)
    after = await scenario.snapshot()
    assert (
        next(row for row in after["run"] if row["id"] == held)["target_held_since"]
        == START
    )


@pytest.mark.asyncio
async def test_start_capacity_other_graphs_consume_quota_without_overlap(
    database: Database,
) -> None:
    scenario = await capacity_scenario(database)
    await scenario.policy({"concurrency": {"max_pending_runs": 1}})
    async with database.transaction() as connection:
        seed = Seeder(connection)
        other_graph = await seed.graph(scenario.namespace_id, "other")
        version = await seed.graph_version(other_graph)
        await seed.run(scenario.namespace_id, other_graph, version, "pending")

    result = await scenario.read("skip")

    assert result == StartCapacity(CapacityOutcome.FULL, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority", ["service", "no_grant", "foreign_graph", "missing_graph", "invalid_id"]
)
async def test_start_capacity_requires_scope_and_locks(
    database: Database, authority: str
) -> None:
    scenario = await capacity_scenario(database)
    if authority in {"service", "no_grant"}:
        values = (
            {"kind": "service", "runtime": "local"}
            if authority == "service"
            else {"namespaces": ()}
        )
        scenario = replace(
            scenario,
            principal=Principal.model_validate(
                scenario.principal.model_dump() | values
            ),
        )
    elif authority == "foreign_graph":
        async with database.transaction() as connection:
            seed = Seeder(connection)
            foreign_namespace = await seed.namespace("foreign")
            other_graph = await seed.graph(foreign_namespace)
        scenario = replace(scenario, graph_id=other_graph)
    elif authority == "missing_graph":
        scenario = replace(scenario, graph_id=scenario.version_id)
    before = await scenario.snapshot()

    async with database.transaction() as connection:
        locks = await scenario.acquire(connection)
        if authority == "invalid_id":
            scenario = replace(scenario, graph_id=PRIVATE)
        async with observed_statements(connection) as statements:
            result = await scenario.check(connection, locks)

    assert result == StartCapacity(CapacityOutcome.REFUSED)
    assert len(statements) == (
        1 if authority in {"foreign_graph", "missing_graph"} else 0
    )
    assert before == await scenario.snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", [ScopeKind.GLOBAL, ScopeKind.NAMESPACE, ScopeKind.GRAPH]
)
async def test_start_capacity_requires_every_scope(
    database: Database, missing: ScopeKind
) -> None:
    scenario = await capacity_scenario(database)

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(scenario.run_id)
        await locks.acquire_scopes(
            tuple(scope for scope in scenario.scopes() if scope.kind is not missing)
        )
        async with observed_statements(connection) as statements:
            with pytest.raises(LockError, match="not held"):
                await scenario.check(connection, locks)

    assert statements == []


@pytest.mark.asyncio
async def test_start_capacity_requires_exact_live_connection_and_run(
    database: Database,
) -> None:
    scenario = await capacity_scenario(database)

    async with database.transaction() as connection:
        locks = await scenario.acquire(connection)
        async with database.read_transaction() as reader:
            with pytest.raises(LockError, match="does not own"):
                await scenario.check(reader, locks)
        with pytest.raises(LockError, match="not held"):
            await replace(scenario, run_id=scenario.version_id).check(connection, locks)
        with pytest.raises(WriterTransactionError, match="not current"):
            await asyncio.create_task(scenario.check(connection, locks))
    async with database.transaction() as connection:
        with pytest.raises(WriterTransactionError, match="not current"):
            await scenario.check(connection, locks)

    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_start_capacity_requires_explicit_overlap(database: Database) -> None:
    scenario = await capacity_scenario(database)

    with pytest.raises(ValueError, match="overlap is invalid"):
        await scenario.read(cast("Overlap", PRIVATE))

    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["insert", "cancel", "lower", "raise"])
async def test_start_capacity_observes_serialized_changes(
    database: Database, change: str
) -> None:
    scenario = await capacity_scenario(database)
    await scenario.policy(
        {"concurrency": {"max_pending_runs": 2 if change == "lower" else 1}}
    )
    async with database.transaction() as connection:
        seed = Seeder(connection)
        predecessor = await seed.run(
            scenario.namespace_id,
            scenario.graph_id,
            scenario.version_id,
            "cancelled" if change == "insert" else "pending",
        )
    ready = asyncio.Event()
    release = asyncio.Event()

    async def owner() -> None:
        async with database.transaction() as connection:
            locks = await replace(scenario, run_id=predecessor).acquire(connection)
            if change == "insert":
                values = Seeder(connection).run_values(
                    scenario.namespace_id,
                    scenario.graph_id,
                    scenario.version_id,
                    "pending",
                )
                await connection.execute(insert(run).values(**values))
            elif change == "cancel":
                seed = Seeder(connection)
                cancelled = await cancel_pending_run(
                    connection,
                    locks,
                    scenario.principal,
                    scenario.namespace_id,
                    scenario.graph_id,
                    predecessor,
                    seed.clock.now,
                    seed.generator,
                )
                assert cancelled.outcome is CancellationOutcome.APPLIED
            else:
                await connection.execute(
                    update(namespace)
                    .where(namespace.c.id == scenario.namespace_id)
                    .values(
                        policy={
                            "concurrency": {
                                "max_pending_runs": 1 if change == "lower" else 3
                            }
                        },
                        policy_revision=2,
                    )
                )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    holder = asyncio.create_task(owner())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower = asyncio.create_task(scenario.read())
    try:
        await wait_for_lock_waiter(database)
        blocked = not follower.done()
    finally:
        release.set()
        await asyncio.wait_for(holder, DEADLINE)
    result = await asyncio.wait_for(follower, DEADLINE)

    assert blocked
    assert (
        result
        == {
            "insert": StartCapacity(CapacityOutcome.FULL, 1, 1),
            "cancel": StartCapacity(CapacityOutcome.ROOM, 1, 0),
            "lower": StartCapacity(CapacityOutcome.FULL, 1, 1),
            "raise": StartCapacity(CapacityOutcome.ROOM, 3, 1),
        }[change]
    )
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_start_capacity_has_no_durable_effects(database: Database) -> None:
    scenario = await capacity_scenario(database)
    async with database.transaction() as connection:
        seed = Seeder(connection)
        await seed.runs(
            scenario.namespace_id, scenario.graph_id, scenario.version_id, 3
        )
    before = await scenario.snapshot()

    full = await scenario.read()
    skipped = await scenario.read("skip")
    after = await scenario.snapshot()

    assert full == StartCapacity(CapacityOutcome.FULL, 2, 3)
    assert skipped == StartCapacity(CapacityOutcome.SKIPPED)
    assert before == after
    assert after["inbound_event"] == after["event"] == after["idempotency_record"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["rollback", "cancel", "shutdown"])
async def test_start_capacity_releases_transaction_resources(
    database: Database, ending: str
) -> None:
    scenario = await capacity_scenario(database)
    before = await scenario.snapshot()
    owned = Database(database.settings)
    ledger = ConnectionLedger(owned)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def reader() -> StartCapacity:
        async with owned.transaction() as connection:
            result = await scenario.check(
                connection, await scenario.acquire(connection)
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if ending == "rollback":
                raise DeliberateRollback()
        return result

    task = asyncio.create_task(reader())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    try:
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "rollback":
            release.set()
            with pytest.raises(DeliberateRollback):
                await task
        else:
            closing = asyncio.create_task(owned.close())
            await until(lambda: owned.lifecycle is Lifecycle.CLOSING)
            release.set()
            assert await task == StartCapacity(CapacityOutcome.ROOM, 2, 0)
            await asyncio.wait_for(closing, DEADLINE)
    finally:
        release.set()
        await owned.close()

    assert before == await scenario.snapshot()
    assert owned.units_in_flight() == ledger.outstanding == 0
    assert ledger.opened == ledger.closed
    assert ledger.closed_while_checked_out == 0
    assert await lock_inventory(database) == []
