import asyncio
from dataclasses import replace

import pytest
from pydantic import MISSING
from sqlalchemy import event, update

from kora.db.connection import Database
from kora.db.retention import RetentionSnapshot
from kora.db.tables import namespace
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.engine.start_capacity import CapacityOutcome, StartCapacity
from kora.ir.runtime import Retention
from kora.protocols.authentication import Principal
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.capacity_support import PRIVATE, capacity_scenario
from tests.engine.lock_support import DEADLINE, wait_for_lock_waiter
from tests.engine.test_start_replay import observed_statements


class NonNativeRetention(Retention):
    pass


@pytest.mark.asyncio
async def test_start_retention_reuses_policy_read_and_captures_pinned_fields(
    database: Database,
) -> None:
    scenario = await capacity_scenario(database)
    await scenario.policy(
        {
            "concurrency": {"max_pending_runs": 7},
            "retention": {"transcripts": PRIVATE, "artifact_payloads": "forever"},
            "private": PRIVATE,
        }
    )
    before = await scenario.snapshot()
    ledger = ConnectionLedger(database)
    graph_retention = Retention(transcripts="000d")

    def change_owned_input(*arguments: object) -> None:
        if graph_retention.transcripts == "000d":
            graph_retention.__init__(transcripts="99d")

    async with database.transaction() as connection:
        locks = await scenario.acquire(connection)
        checkouts = ledger.checkouts
        async with observed_statements(connection) as capacity_statements:
            capacity_only = await scenario.check(connection, locks)
        event.listen(
            connection.sync_connection, "before_cursor_execute", change_owned_input
        )
        try:
            async with observed_statements(connection) as retention_statements:
                opted_in = await scenario.check(
                    connection, locks, graph_retention=graph_retention
                )
        finally:
            event.remove(
                connection.sync_connection, "before_cursor_execute", change_owned_input
            )
    checkouts_after = ledger.checkouts
    after = await scenario.snapshot()

    assert graph_retention.transcripts == "99d"
    assert capacity_only == StartCapacity(CapacityOutcome.ROOM, 7, 0)
    assert opted_in == StartCapacity(
        CapacityOutcome.ROOM, 7, 0, RetentionSnapshot("0d", "forever")
    )
    assert capacity_statements == retention_statements
    assert sum("namespace.policy" in query for query in retention_statements) == 1
    assert checkouts_after == checkouts
    assert before == after
    assert database.writer_is_free()
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_start_retention_distinguishes_absent_and_invalid_policy(
    database: Database,
) -> None:
    scenario = await capacity_scenario(database)
    ledger = ConnectionLedger(database)
    await scenario.policy({})
    inherited = await scenario.read(graph_retention=MISSING)
    invalid_overrides = await scenario.read(graph_retention=NonNativeRetention())
    await scenario.policy({"retention": None})
    before = await scenario.snapshot()

    refused = await scenario.read(graph_retention=MISSING)
    old_caller = await scenario.read()
    after = await scenario.snapshot()

    assert inherited == StartCapacity(
        CapacityOutcome.ROOM, 1000, 0, RetentionSnapshot("30d", "30d")
    )
    assert refused == StartCapacity(CapacityOutcome.RETENTION_UNAVAILABLE)
    assert invalid_overrides == refused
    assert old_caller == StartCapacity(CapacityOutcome.ROOM, 1000, 0)
    assert before == after
    assert database.writer_is_free()
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "graph_retention",
    [MISSING, NonNativeRetention()],
    ids=["omitted", "non-native-override"],
)
async def test_start_retention_preserves_overlap_and_quota_before_resolution(
    database: Database, graph_retention: Retention | MISSING
) -> None:
    scenario = await capacity_scenario(database)
    ledger = ConnectionLedger(database)
    await scenario.policy(
        {"concurrency": {"max_pending_runs": 1}, "retention": {"transcripts": PRIVATE}}
    )
    async with database.transaction() as connection:
        await Seeder(connection).run(
            scenario.namespace_id, scenario.graph_id, scenario.version_id, "pending"
        )
    before = await scenario.snapshot()

    async with database.transaction() as connection:
        locks = await scenario.acquire(connection)
        async with observed_statements(connection) as statements:
            skipped = await scenario.check(
                connection, locks, "skip", graph_retention=graph_retention
            )
        full = await scenario.check(connection, locks, graph_retention=graph_retention)
    after = await scenario.snapshot()

    assert skipped == StartCapacity(CapacityOutcome.SKIPPED)
    assert all("policy" not in query for query in statements)
    assert full == StartCapacity(CapacityOutcome.FULL, 1, 1)
    assert before == after
    assert database.writer_is_free()
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_graph", [False, True], ids=["no-grant", "foreign"])
async def test_start_retention_refuses_scope_before_policy(
    database: Database, foreign_graph: bool
) -> None:
    scenario = await capacity_scenario(database)
    ledger = ConnectionLedger(database)
    if foreign_graph:
        async with database.transaction() as connection:
            seeder = Seeder(connection)
            foreign_namespace = await seeder.namespace("other")
            foreign = await seeder.graph(foreign_namespace)
        scenario = replace(scenario, graph_id=foreign)
    else:
        principal = Principal.model_validate(
            scenario.principal.model_dump() | {"namespaces": ()}
        )
        scenario = replace(scenario, principal=principal)

    async with database.transaction() as connection:
        locks = await scenario.acquire(connection)
        async with observed_statements(connection) as statements:
            result = await scenario.check(connection, locks, graph_retention=MISSING)

    assert result == StartCapacity(CapacityOutcome.REFUSED)
    assert len(statements) == int(foreign_graph)
    assert all("policy" not in query for query in statements)
    assert database.writer_is_free()
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_start_retention_observes_same_committed_policy_as_capacity(
    database: Database,
) -> None:
    scenario = await capacity_scenario(database)
    ledger = ConnectionLedger(database)
    await scenario.policy(
        {"concurrency": {"max_pending_runs": 2}, "retention": {"transcripts": "7d"}}
    )
    ready = asyncio.Event()
    release = asyncio.Event()

    async def update_policy() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_scopes(
                (Scope(ScopeKind.NAMESPACE, (scenario.namespace_id,)),)
            )
            await connection.execute(
                update(namespace)
                .where(namespace.c.id == scenario.namespace_id)
                .values(
                    policy={
                        "concurrency": {"max_pending_runs": 5},
                        "retention": {"transcripts": "8d", "artifact_payloads": "0d"},
                    },
                    policy_revision=2,
                )
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    writer = asyncio.create_task(update_policy())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower = asyncio.create_task(scenario.read(graph_retention=MISSING))
    try:
        await wait_for_lock_waiter(database)
        waiting = not follower.done()
    finally:
        release.set()
        await asyncio.wait_for(writer, DEADLINE)
    result = await asyncio.wait_for(follower, DEADLINE)

    assert waiting
    assert result == StartCapacity(
        CapacityOutcome.ROOM, 5, 0, RetentionSnapshot("8d", "0d")
    )
    assert database.writer_is_free()
    assert ledger.outstanding == database.units_in_flight() == 0
