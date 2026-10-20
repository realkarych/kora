import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import Select, exists, select, text, true, update
from sqlalchemy.exc import IntegrityError

from kora.db.connection import Database, Dialect
from kora.db.reads import (
    MarkedRunSweep,
    PageSizeError,
    events_of_run,
    execution_run_usage,
    graph_by_name,
    graph_version_by_number,
    has_physical_attempt,
    has_unfinished_top_level_instance,
    marked_run_page,
    namespace_by_id,
    namespace_by_name,
    namespace_outstanding_usage,
    node_instances_of_run,
    run_by_id,
    target_holders,
    token_by_digest,
    worker_by_id,
)
from kora.db.tables import (
    FINISHED_NODE_STATUSES,
    api_token,
    event,
    node_instance,
    run,
)
from tests.db.seed import START, Seeder

MARKED_RUNS: int = 1000
UNMARKED_RUNS: int = 50
PAGE_SIZE: int = 100
TOP_LEVEL_NODES: int = 10
BODY_NODES: int = 1000
EVENTS: int = 250
OTHER_EVENTS: int = 3000
FINISH_INDEX: str = "ix_node_instance_run_top_level"
SWEEP_INDEX: str = "ix_run_needs_scheduling"


async def query_plan(database: Database, statement: Select[object]) -> str:
    prefix = "EXPLAIN QUERY PLAN " if database.dialect is Dialect.SQLITE else "EXPLAIN "
    compiled = statement.compile(
        dialect=database.reader_engine.dialect, compile_kwargs={"literal_binds": True}
    )
    async with database.read_transaction() as connection:
        if database.dialect is Dialect.POSTGRESQL:
            await connection.execute(text("ANALYZE"))
        rows = (await connection.execute(text(prefix + str(compiled)))).all()
    return "\n".join(" ".join(str(value) for value in row) for row in rows)


async def seeded_graph(database: Database) -> tuple[str, str, str]:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
    return namespace_id, graph_id, version_id


@pytest.mark.asyncio
async def test_marked_run_sweep_wraps_behind_the_cursor(database: Database) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        marked = await seeder.runs(namespace_id, graph_id, version_id, MARKED_RUNS)
        await seeder.runs(namespace_id, graph_id, version_id, UNMARKED_RUNS, False)
    sweep = MarkedRunSweep(PAGE_SIZE)
    visited: list[str] = []
    late_id: str | None = None

    async with database.read_transaction() as connection:
        first_page = await sweep.next_page(connection)
    visited.extend(first_page.run_ids)
    async with database.transaction() as connection:
        seeder = Seeder.at(connection, START - timedelta(days=1))
        late_id = await seeder.run(namespace_id, graph_id, version_id)
    while sweep.ceiling is not None:
        async with database.read_transaction() as connection:
            page = await sweep.next_page(connection)
        visited.extend(page.run_ids)
    async with database.read_transaction() as connection:
        after_wrap = await sweep.next_page(connection)
    plan = await query_plan(
        database,
        select(run.c.id)
        .where(run.c.needs_scheduling == true(), run.c.id <= marked[-1])
        .order_by(run.c.id)
        .limit(PAGE_SIZE),
    )

    assert late_id is not None
    assert late_id < first_page.run_ids[0]
    assert sorted(visited) == sorted(marked)
    assert len(visited) == MARKED_RUNS
    assert after_wrap.wrapped
    assert after_wrap.run_ids[0] == late_id
    assert SWEEP_INDEX in plan
    assert any(
        "id<" in line or "id <" in line or "id)::text <" in line
        for line in plan.splitlines()
    )
    with pytest.raises(PageSizeError):
        MarkedRunSweep(0)


@pytest.mark.asyncio
async def test_marked_run_sweep_discovers_commit_behind_scan(
    database: Database,
) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    async with database.transaction() as connection:
        marked = await Seeder(connection).runs(namespace_id, graph_id, version_id, 3)
    sweep = MarkedRunSweep(PAGE_SIZE)

    if database.dialect is Dialect.POSTGRESQL:
        page_taken = asyncio.Event()
        inserted: list[str] = []

        async def slow_writer() -> None:
            async with database.transaction() as connection:
                seeder = Seeder.at(connection, START - timedelta(days=1))
                inserted.append(await seeder.run(namespace_id, graph_id, version_id))
                await asyncio.wait_for(page_taken.wait(), 10)

        writer = asyncio.create_task(slow_writer())
        while not inserted:
            await asyncio.sleep(0)
        async with database.read_transaction() as connection:
            page = await sweep.next_page(connection)
        page_taken.set()
        await writer
        behind = inserted[0]
    else:
        async with database.read_transaction() as connection:
            page = await sweep.next_page(connection)
        async with database.transaction() as connection:
            seeder = Seeder.at(connection, START - timedelta(days=1))
            behind = await seeder.run(namespace_id, graph_id, version_id)
    async with database.read_transaction() as connection:
        next_sweep = await sweep.next_page(connection)

    assert sorted(page.run_ids) == sorted(marked)
    assert behind not in page.run_ids
    assert behind in next_sweep.run_ids
    assert next_sweep.wrapped


@pytest.mark.asyncio
async def test_run_finish_check_uses_top_level_instances(database: Database) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        run_id = await seeder.run(namespace_id, graph_id, version_id)
        other_run = await seeder.run(namespace_id, graph_id, version_id)
        top_ids = await seeder.nodes(
            run_id, [f"top{i}" for i in range(TOP_LEVEL_NODES)], "succeeded", True
        )
        await seeder.nodes(
            run_id, [f"fan[{i}]@1.body" for i in range(BODY_NODES)], "running", False
        )
        await seeder.nodes(other_run, ["pending-elsewhere"], "pending", True)

    async with database.read_transaction() as connection:
        finished_with_live_body = not await has_unfinished_top_level_instance(
            connection, run_id
        )
        other_unfinished = await has_unfinished_top_level_instance(
            connection, other_run
        )
    async with database.transaction() as connection:
        await connection.execute(
            update(node_instance)
            .where(node_instance.c.id == top_ids[-1])
            .values(status="waiting")
        )
    async with database.read_transaction() as connection:
        unfinished_after_change = await has_unfinished_top_level_instance(
            connection, run_id
        )
    plan = await query_plan(
        database,
        select(
            exists().where(
                node_instance.c.run_id == run_id,
                node_instance.c.top_level.is_(True),
                node_instance.c.status.not_in(FINISHED_NODE_STATUSES),
            )
        ),
    )

    assert finished_with_live_body
    assert other_unfinished
    assert unfinished_after_change
    index_lines = [line for line in plan.splitlines() if FINISH_INDEX in line]
    assert index_lines
    condition = "\n".join(
        line for line in plan.splitlines() if "run_id" in line and "top_level" in line
    )
    assert condition


@pytest.mark.asyncio
async def test_occupancy_counts_follow_the_exact_predicates(
    database: Database,
) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        other_namespace = await seeder.namespace("other")
        other_graph = await seeder.graph(other_namespace)
        other_version = await seeder.graph_version(other_graph)
        pending = await seeder.run(namespace_id, graph_id, version_id, "pending")
        running_without_attempt = await seeder.run(
            namespace_id, graph_id, version_id, "running"
        )
        settled = await seeder.run(namespace_id, graph_id, version_id, "failed")
        failed_with_attempt = await seeder.run(
            namespace_id, graph_id, version_id, "failed"
        )
        held = await seeder.run(
            namespace_id,
            graph_id,
            version_id,
            "succeeded",
            target_key="svc/one",
            target_held_since=START,
        )
        foreign_running = await seeder.run(
            other_namespace, other_graph, other_version, "running"
        )
        live_node = await seeder.node(failed_with_attempt, "a")
        await seeder.attempt(live_node, "running", 1)
        await seeder.attempt(live_node, "failed", 2)
        settled_node = await seeder.node(settled, "a")
        await seeder.attempt(settled_node, "failed", 1)
        foreign_node = await seeder.node(foreign_running, "a")
        await seeder.attempt(foreign_node, "created", 1)

    async with database.read_transaction() as connection:
        physical = {
            name: await has_physical_attempt(connection, run_id)
            for name, run_id in {
                "pending": pending,
                "running_without_attempt": running_without_attempt,
                "settled": settled,
                "failed_with_attempt": failed_with_attempt,
                "held": held,
            }.items()
        }
        execution = await execution_run_usage(connection, graph_id)
        foreign_execution = await execution_run_usage(connection, other_graph)
        outstanding = await namespace_outstanding_usage(connection, namespace_id)
        foreign_outstanding = await namespace_outstanding_usage(
            connection, other_namespace
        )
        holders = await target_holders(connection, namespace_id, "svc/one")
        foreign_holders = await target_holders(connection, other_namespace, "svc/one")

    assert physical == {
        "pending": False,
        "running_without_attempt": False,
        "settled": False,
        "failed_with_attempt": True,
        "held": False,
    }
    assert (execution, foreign_execution) == (1, 1)
    assert (outstanding, foreign_outstanding) == (4, 1)
    assert [holder.run_id for holder in holders] == [held]
    assert holders[0].target_held_since == START
    assert foreign_holders == []


@pytest.mark.asyncio
async def test_registry_reads_are_namespace_scoped(database: Database) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        other_namespace = await seeder.namespace("other")
        run_id = await seeder.run(namespace_id, graph_id, version_id)
        node_id = await seeder.node(run_id, "step")
        event_ids = await seeder.events(namespace_id, run_id, EVENTS)
        other_run = await seeder.run(namespace_id, graph_id, version_id)
        await seeder.events(namespace_id, other_run, OTHER_EVENTS)
        await seeder.events(other_namespace, None, OTHER_EVENTS)
        token_id = await seeder.token("a" * 64, kind="service", owner="worker-1")
        worker_id = await seeder.worker(token_id)

    async with database.read_transaction() as connection:
        own = (
            await namespace_by_name(connection, "default"),
            await namespace_by_id(connection, namespace_id),
            await graph_by_name(connection, namespace_id, "pipeline"),
            await graph_version_by_number(connection, namespace_id, graph_id, 1),
            await run_by_id(connection, namespace_id, run_id),
            await node_instances_of_run(connection, namespace_id, run_id),
            await worker_by_id(connection, worker_id),
        )
        foreign = (
            await graph_by_name(connection, other_namespace, "pipeline"),
            await graph_version_by_number(connection, other_namespace, graph_id, 1),
            await run_by_id(connection, other_namespace, run_id),
            await node_instances_of_run(connection, other_namespace, run_id),
            await events_of_run(connection, other_namespace, run_id),
        )
        first_page = await events_of_run(connection, namespace_id, run_id, None, 100)
        second_page = await events_of_run(
            connection, namespace_id, run_id, first_page[-1].id, 100
        )
        third_page = await events_of_run(
            connection, namespace_id, run_id, second_page[-1].id, 100
        )
        missing = (
            await namespace_by_name(connection, "missing"),
            await run_by_id(connection, namespace_id, "0" * 26),
            await worker_by_id(connection, "worker-2"),
        )
    plan = await query_plan(
        database,
        select(event.c.id)
        .where(event.c.namespace_id == namespace_id, event.c.run_id == run_id)
        .order_by(event.c.id)
        .limit(100),
    )

    assert own[0] is not None
    assert own[0].id == namespace_id
    assert own[1] is not None
    assert own[1].policy == {"max_pending_runs": 1000}
    assert own[2] is not None
    assert own[2].id == graph_id
    assert own[3] is not None
    assert own[3].id == version_id
    assert own[4] is not None
    assert own[4].id == run_id
    assert [node.id for node in own[5]] == [node_id]
    assert own[6] is not None
    assert own[6].token_id == token_id
    assert foreign == (None, None, None, [], [])
    assert [row.id for row in first_page + second_page + third_page] == event_ids
    assert [row.payload for row in third_page][-1] == {"index": EVENTS - 1}
    assert missing == (None, None, None)
    assert "ix_event_run_id_id" in plan
    with pytest.raises(PageSizeError):
        await events_of_run(connection, namespace_id, run_id, None, 0)
    with pytest.raises(PageSizeError):
        await marked_run_page(connection, None, "z", 1001)


@pytest.mark.asyncio
async def test_token_digest_lookup_is_exact_and_unique(database: Database) -> None:
    digest = "ab" * 32
    async with database.transaction() as connection:
        token_id = await Seeder(connection).token(digest, expires_at=START)

    async with database.read_transaction() as connection:
        found = await token_by_digest(connection, digest)
        by_case = await token_by_digest(connection, digest.upper())
        by_neighbour = await token_by_digest(connection, "ab" * 31 + "ac")
        by_space = await token_by_digest(connection, digest + " ")
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await Seeder(connection).token(digest)
    async with database.read_transaction() as connection:
        count = len((await connection.execute(api_token.select())).all())

    assert found is not None
    assert (found.id, found.digest, found.approve, found.expires_at) == (
        token_id,
        digest,
        True,
        START,
    )
    assert (by_case, by_neighbour, by_space) == (None, None, None)
    assert count == 1
