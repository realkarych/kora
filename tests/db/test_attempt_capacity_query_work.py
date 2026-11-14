from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from kora.db.attempt_capacity import (
    AttemptCapacity,
    GraphCapSource,
    RunRecovery,
    read_attempt_capacity,
    read_graph_cap_source,
    read_run_recovery,
)
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt, graph_version, node_instance, run
from tests.db.seed import Seeder
from tests.db.test_expiry_query_work import query_plan
from tests.db.test_graph_read_admission import executed_queries


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [300, 3000])
async def test_capacity_reads_preserve_results_with_retained_history(
    database: Database, history: int, tmp_path: Path
) -> None:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        first_version = await seed.graph_version(graph_id)
        run_id = await seed.run(namespace_id, graph_id, first_version)
        node_id = await seed.node(run_id, "candidate")
        active = [
            await seed.attempt(node_id, "created", number) for number in range(1, 26)
        ]
        await writer.execute(
            update(attempt)
            .where(attempt.c.id.in_(active))
            .values(recovery_required=True)
        )
        with await writer.execute(
            select(attempt).where(attempt.c.id == active[0])
        ) as result:
            attempt_values = dict(result.mappings().one())
        with await writer.execute(
            select(graph_version).where(graph_version.c.id == first_version)
        ) as result:
            version_values = dict(result.mappings().one())
        with await writer.execute(
            select(node_instance).where(node_instance.c.id == node_id)
        ) as result:
            node_values = dict(result.mappings().one())
        with await writer.execute(select(run).where(run.c.id == run_id)) as result:
            run_values = dict(result.mappings().one())
        last_version = first_version
        for offset in range(0, history, 100):
            attempts: list[dict[str, object]] = []
            versions: list[dict[str, object]] = []
            runs: list[dict[str, object]] = []
            nodes: list[dict[str, object]] = []
            for number in range(offset + 1, offset + 101):
                old_run = seed.new_id()
                old_node = seed.new_id()
                runs.append(
                    {
                        **run_values,
                        "id": old_run,
                        "idempotency_key": old_run,
                        "status": "succeeded",
                    }
                )
                nodes.append(
                    {
                        **node_values,
                        "id": old_node,
                        "run_id": old_run,
                        "status": "succeeded",
                    }
                )
                attempts.append(
                    {
                        **attempt_values,
                        "id": seed.new_id(),
                        "number": number + 25,
                        "node_instance_id": node_id if number % 2 else old_node,
                        "status": "succeeded",
                        "recovery_required": False,
                        "recovery": {"retained": "r" * 4096},
                    }
                )
                last_version = seed.new_id()
                versions.append(
                    {
                        **version_values,
                        "id": last_version,
                        "number": number + 1,
                        "status": "retired",
                        "ir": {"retained": "g" * 4096},
                    }
                )
            await writer.execute(insert(run), runs)
            await writer.execute(insert(node_instance), nodes)
            await writer.execute(insert(attempt), attempts)
            await writer.execute(insert(graph_version), versions)
    postgres = database.dialect is Dialect.POSTGRESQL
    async with database.transaction() as writer:
        for table in ("attempt", "node_instance", "run", "graph_version"):
            await writer.exec_driver_sql("ANALYZE " + table)
        if postgres:
            await writer.exec_driver_sql(
                "SET LOCAL plan_cache_mode = force_generic_plan"
            )

        with executed_queries(writer) as queries:
            capacity = await read_attempt_capacity(
                writer,
                namespace_id=namespace_id,
                graph_id=graph_id,
                run_id=run_id,
                runtime="local",
            )
            recovery = await read_run_recovery(writer, run_id)
            active_source = await read_graph_cap_source(writer, graph_id)
        plans = [await query_plan(writer, postgres, query) for query in queries]
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == first_version)
            .values(status="retired")
        )
        with executed_queries(writer) as fallback_queries:
            fallback_source = await read_graph_cap_source(writer, graph_id)
        plans.extend(
            [await query_plan(writer, postgres, query) for query in fallback_queries]
        )
    (tmp_path / "capacity-native-plans.txt").write_text(
        "\n\n".join("\n".join(plan) for plan in plans)
    )

    assert capacity == AttemptCapacity(25, 25, 25, 25, 1, 25)
    assert recovery == RunRecovery(25, tuple(sorted(active)[:20]))
    assert active_source == GraphCapSource(first_version, 1)
    assert fallback_source == GraphCapSource(last_version, history + 1)
    assert database.units_in_flight() == 0
