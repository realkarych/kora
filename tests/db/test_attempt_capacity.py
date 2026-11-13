import pytest
from sqlalchemy import update

from kora.db.attempt_capacity import (
    AttemptCapacity,
    GraphCapSource,
    RunRecovery,
    read_attempt_capacity,
    read_graph_cap_source,
    read_run_recovery,
)
from kora.db.connection import Database
from kora.db.tables import attempt, graph_version, node_instance
from tests.db.seed import Seeder


@pytest.mark.asyncio
async def test_attempt_capacity_counts_physical_history_across_graph_versions(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        first_version = await seed.graph_version(graph_id)
        second_version = await seed.graph_version(graph_id, 2)
        first_run = await seed.run(namespace_id, graph_id, first_version, "failed")
        second_run = await seed.run(namespace_id, graph_id, second_version)
        first_node = await seed.node(first_run, "old", "cancelled")
        second_node = await seed.node(second_run, "current", "queued")
        statuses = ("created", "claimed", "submitted", "running", "finishing")
        first_attempts = [
            await seed.attempt(first_node, status, index)
            for index, status in enumerate(statuses, 1)
        ]
        await seed.attempt(second_node, "created")
        await seed.attempt(second_node, "running", 2)
        terminal = [
            await seed.attempt(first_node, status, index)
            for index, status in enumerate(
                ("succeeded", "failed", "cancelled", "lost"), 6
            )
        ]
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == first_node)
            .values(current_attempt_id=terminal[-1])
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == first_attempts[-1])
            .values(runtime="another-runtime", desired_state="cancel")
        )

    async with database.read_transaction() as reader:
        observed = await read_attempt_capacity(
            reader,
            namespace_id=namespace_id,
            graph_id=graph_id,
            run_id=first_run,
            runtime="local",
        )

    assert observed == AttemptCapacity(7, 7, 7, 6, 2, 5)


@pytest.mark.asyncio
async def test_attempt_capacity_keeps_each_scope_independent(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        candidate_run = await seed.run(namespace_id, graph_id, version_id)
        await seed.node(candidate_run, "approval", "waiting")
        other_graph = await seed.graph(namespace_id, "another")
        other_version = await seed.graph_version(other_graph)
        other_run = await seed.run(namespace_id, other_graph, other_version)
        other_node = await seed.node(other_run, "other")
        await seed.attempt(other_node, "created")
        foreign_namespace = await seed.namespace("foreign")
        foreign_graph = await seed.graph(foreign_namespace)
        foreign_version = await seed.graph_version(foreign_graph)
        foreign_run = await seed.run(foreign_namespace, foreign_graph, foreign_version)
        foreign_node = await seed.node(foreign_run, "foreign")
        foreign_attempt = await seed.attempt(foreign_node, "running")
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == foreign_attempt)
            .values(runtime="other")
        )

    async with database.read_transaction() as reader:
        observed = await read_attempt_capacity(
            reader,
            namespace_id=namespace_id,
            graph_id=graph_id,
            run_id=candidate_run,
            runtime="local",
        )

    assert observed == AttemptCapacity(2, 1, 0, 1, 0, 0)


@pytest.mark.asyncio
async def test_run_recovery_keeps_all_history_and_bounds_only_the_sample(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        run_id = await seed.run(namespace_id, graph_id, version_id)
        node_id = await seed.node(run_id, "current")
        blockers = [
            await seed.attempt(node_id, "finishing", number) for number in range(1, 26)
        ]
        current = await seed.attempt(node_id, "running", 26)
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
        healthy_run = await seed.run(namespace_id, graph_id, version_id)
        healthy_node = await seed.node(healthy_run, "healthy")
        await seed.attempt(healthy_node, "running")

    async with database.read_transaction() as reader:
        blocked = await read_run_recovery(reader, run_id)
        healthy = await read_run_recovery(reader, healthy_run)

    assert blocked == RunRecovery(25, tuple(sorted(blockers)[:20]))
    assert healthy == RunRecovery(0, ())


@pytest.mark.asyncio
async def test_graph_cap_source_prefers_highest_active_then_highest_deployed(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        first = await seed.graph_version(graph_id)
        second = await seed.graph_version(graph_id, 2)
        third = await seed.graph_version(graph_id, 3)
        other = await seed.graph(namespace_id, "empty")
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == third)
            .values(status="retired")
        )

    async with database.read_transaction() as reader:
        active = await read_graph_cap_source(reader, graph_id)
        absent = await read_graph_cap_source(reader, other)
    async with database.transaction() as writer:
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id.in_((first, second)))
            .values(status="retired")
        )
        fallback = await read_graph_cap_source(writer, graph_id)

    assert active == GraphCapSource(second, 2)
    assert absent is None
    assert fallback == GraphCapSource(third, 3)
