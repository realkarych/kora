import asyncio
from dataclasses import replace
from typing import Literal, cast

import pytest
from sqlalchemy import select, text, update

from kora.db.attempt_capacity import RunRecovery
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt, graph_version, node_instance
from kora.engine.attempt_capacity import (
    AttemptCapacityRefused,
    CapacityLimit,
    check_attempt_capacity,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import LockError
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.attempt_capacity_support import (
    capacity_case,
    live_caps,
    namespace_limit,
)
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limiting", ["global", "namespace", "nodes", "runs", "runtime"]
)
async def test_capacity_reports_first_limiting_scope(
    database: Database, limiting: str
) -> None:
    ordered = ("global", "namespace", "nodes", "runs", "runtime")
    index = ordered.index(limiting)
    case = await capacity_case(
        database,
        {
            "max_running_nodes": 1 if index <= 2 else 2,
            "max_running_runs": 1 if index <= 3 else 2,
        },
    )
    await namespace_limit(database, case.scope.namespace_id, 1 if index <= 1 else None)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        old_version = await seed.graph_version(case.scope.graph_id, 0)
        old_run = await seed.run(
            case.scope.namespace_id, case.scope.graph_id, old_version, "cancelled"
        )
        old_node = await seed.node(old_run, "old", "cancelled")
        await seed.attempt(old_node, "finishing")
        current = await seed.attempt(old_node, "succeeded", 2)
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == old_node)
            .values(current_attempt_id=current)
        )

    async with database.transaction() as writer:
        observed = await check_attempt_capacity(
            await case.context(writer),
            "local",
            case.caps,
            global_limit=1 if index == 0 else None,
            runtime_limit=1,
        )

    assert isinstance(observed, CapacityLimit)
    assert observed.scope == ("graph" if limiting in ("nodes", "runs") else limiting)
    assert (
        observed.name
        == {
            "global": "max_running_attempts",
            "namespace": "max_running_attempts",
            "nodes": "max_running_nodes",
            "runs": "max_running_runs",
            "runtime": "max_concurrent",
        }[limiting]
    )
    assert observed.limit == observed.used == 1
    assert observed.source_graph_version_id == (
        case.scope.version_id if limiting in ("nodes", "runs") else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied", [False, True])
async def test_lowered_graph_run_limit_blocks_only_positive_additions(
    database: Database, occupied: bool
) -> None:
    case = await capacity_case(database, {"max_running_runs": 1})
    async with database.transaction() as writer:
        seed = Seeder(writer)
        peer = await seed.run(
            case.scope.namespace_id, case.scope.graph_id, case.scope.version_id
        )
        await seed.attempt(await seed.node(peer, "peer"), "created")
        if occupied:
            await seed.attempt(
                await seed.node(case.run_id, "historical", "cancelled"), "finishing"
            )

    async with database.transaction() as writer:
        observed = await check_attempt_capacity(
            await case.context(writer),
            "local",
            case.caps,
            global_limit=None,
            runtime_limit=None,
        )

    assert observed == (
        None
        if occupied
        else CapacityLimit("graph", "max_running_runs", 1, 1, case.scope.version_id)
    )


@pytest.mark.asyncio
async def test_recovery_precedes_capacity_and_does_not_block_healthy_peer(
    database: Database,
) -> None:
    case = await capacity_case(
        database, {"max_running_nodes": 1, "max_running_runs": 1}
    )
    peer = await capacity_case(database)
    await namespace_limit(database, case.scope.namespace_id, 1)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        node_id = await seed.node(case.run_id, "history", "cancelled")
        blockers = [
            await seed.attempt(node_id, "finishing", number) for number in range(1, 24)
        ]
        current = await seed.attempt(node_id, "running", 24)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id.in_(blockers))
            .values(recovery_required=True)
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(current_attempt_id=current)
        )
    async with database.read_transaction() as reader:
        with await reader.execute(
            select(attempt.c.id, attempt.c.status, attempt.c.recovery_required)
        ) as result:
            before = result.all()

    async with database.transaction() as writer:
        observed = await check_attempt_capacity(
            await case.context(writer),
            "local",
            case.caps,
            global_limit=1,
            runtime_limit=1,
        )
    async with database.transaction() as writer:
        healthy = await check_attempt_capacity(
            await peer.context(writer),
            "local",
            peer.caps,
            global_limit=1,
            runtime_limit=1,
        )

    assert observed == RunRecovery(23, tuple(sorted(blockers)[:20]))
    assert healthy == CapacityLimit("global", "max_running_attempts", 1, 24, None)
    assert len(before) == 24
    assert peer.run_id != case.run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("retire", [False, True])
async def test_live_cap_source_changes_independently_of_pinned_execution(
    database: Database, retire: bool
) -> None:
    case = await capacity_case(database)
    async with database.transaction() as writer:
        version_id = Seeder(writer).new_id()
    next_scope = GraphResourceScope(
        case.scope.namespace_id, case.scope.graph_id, version_id
    )
    current = await live_caps(database, next_scope, 2, {"max_running_nodes": 1})
    if retire:
        async with database.transaction() as writer:
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.graph_id == case.scope.graph_id)
                .values(status="retired")
            )

    async with database.transaction() as writer:
        context = await case.context(writer)
        with pytest.raises(AttemptCapacityRefused):
            await check_attempt_capacity(
                context, "local", case.caps, global_limit=None, runtime_limit=None
            )
        observed = await check_attempt_capacity(
            context, "local", current, global_limit=None, runtime_limit=None
        )

    assert observed is None
    assert current.scope.version_id != context.scope.version_id


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["namespace_id", "graph_id", "version_id"])
async def test_capacity_refuses_foreign_parent_binding(
    database: Database, field: str
) -> None:
    case = await capacity_case(database)
    async with database.transaction() as writer:
        foreign = Seeder(writer).new_id()
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        changed = replace(case, scope=replace(case.scope, **{field: foreign}))
        context = await changed.context(writer)
        with pytest.raises(AttemptCapacityRefused) as caught:
            await check_attempt_capacity(
                context, "local", case.caps, global_limit=None, runtime_limit=None
            )

    assert caught.value.__context__ is caught.value.__cause__ is None
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5])
async def test_capacity_rejects_invalid_trusted_limits(
    database: Database, value: object
) -> None:
    case = await capacity_case(database)

    async with database.transaction() as writer:
        context = await case.context(writer)
        with pytest.raises(AttemptCapacityRefused):
            await check_attempt_capacity(
                context,
                "local",
                case.caps,
                global_limit=cast(int, value),
                runtime_limit=None,
            )
        with pytest.raises(AttemptCapacityRefused):
            await check_attempt_capacity(
                context,
                "local",
                case.caps,
                global_limit=None,
                runtime_limit=cast(int, value),
            )

    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_capacity_preserves_large_valid_caps_and_refuses_decode_overflow(
    database: Database,
) -> None:
    case = await capacity_case(
        database, {"max_running_nodes": 10**80, "max_running_runs": 10**80}
    )
    await namespace_limit(database, case.scope.namespace_id, 10**800)

    with pytest.raises(AttemptCapacityRefused) as caught:
        await capacity_case(database, max_ir_bytes=1)
    async with database.transaction() as writer:
        observed = await check_attempt_capacity(
            await case.context(writer),
            "local",
            case.caps,
            global_limit=10**800,
            runtime_limit=10**800,
        )

    assert observed is None
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert case.caps.nodes == case.caps.runs == 10**80


@pytest.mark.asyncio
async def test_capacity_requires_original_connection_and_runtime_scopes(
    database: Database,
) -> None:
    case = await capacity_case(database)

    async with database.transaction() as writer:
        context = await case.context(writer, capacity=False)
        with pytest.raises(LockError):
            await check_attempt_capacity(
                context, "local", case.caps, global_limit=None, runtime_limit=None
            )
    async with database.transaction() as writer:
        context = await case.context(writer)
        with pytest.raises(LockError):
            await check_attempt_capacity(
                context,
                "unheld-runtime",
                case.caps,
                global_limit=None,
                runtime_limit=None,
            )
        async with database.read_transaction() as reader:
            with pytest.raises(LockError):
                await check_attempt_capacity(
                    replace(context, connection=reader),
                    "local",
                    case.caps,
                    global_limit=None,
                    runtime_limit=None,
                )

    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readonly", "repeatable"])
async def test_capacity_refuses_stale_or_readonly_postgres_mode(
    database: Database, mode: str
) -> None:
    case = await capacity_case(database)

    async with database.transaction() as writer:
        if database.dialect is Dialect.POSTGRESQL:
            await writer.execute(
                text(
                    "SET TRANSACTION READ ONLY"
                    if mode == "readonly"
                    else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
            )
        context = await case.context(writer)
        if database.dialect is Dialect.POSTGRESQL:
            with pytest.raises(AttemptCapacityRefused):
                await check_attempt_capacity(
                    context, "local", case.caps, global_limit=1, runtime_limit=1
                )
        else:
            assert (
                await check_attempt_capacity(
                    context, "local", case.caps, global_limit=1, runtime_limit=1
                )
                is None
            )

    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["commit", "rollback", "cancel"])
async def test_capacity_waiter_reads_committed_occupancy_after_release(
    database: Database, ending: Literal["commit", "rollback", "cancel"]
) -> None:
    case = await capacity_case(database)
    entered, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def hold() -> None:
        async with database.transaction() as writer:
            await case.context(writer)
            seed = Seeder(writer)
            await seed.attempt(await seed.node(case.run_id, "reserved"), "created")
            entered.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if ending == "rollback":
                raise DeliberateRollback()

    async def follow() -> CapacityLimit | RunRecovery | None:
        async with database.transaction() as writer:
            return await check_attempt_capacity(
                await case.context(writer),
                "local",
                case.caps,
                global_limit=1,
                runtime_limit=None,
            )

    holder = asyncio.create_task(hold())
    follower: asyncio.Task[CapacityLimit | RunRecovery | None] | None = None
    observed: CapacityLimit | RunRecovery | None = None
    try:
        await asyncio.wait_for(entered.wait(), DEADLINE)
        follower = asyncio.create_task(follow())
        await wait_for_lock_waiter(database)
    finally:
        if ending == "cancel":
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(holder, DEADLINE)
        else:
            release.set()
            if ending == "rollback":
                with pytest.raises(DeliberateRollback):
                    await asyncio.wait_for(holder, DEADLINE)
            else:
                await asyncio.wait_for(holder, DEADLINE)
        if follower is not None:
            observed = await asyncio.wait_for(follower, DEADLINE)

    assert follower is not None
    assert observed == (
        CapacityLimit("global", "max_running_attempts", 1, 1, None)
        if ending == "commit"
        else None
    )
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
