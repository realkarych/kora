import pytest
from sqlalchemy import insert, select

from kora.db.connection import Database, Dialect
from kora.db.grant_history import read_history_rows
from kora.db.tables import attempt_dispatcher, node_instance, run
from kora.db.types import JsonObject, as_json_value
from kora.db.worker_history import scoped_history_attempt
from kora.protocols.worker_claim_system import PROCESS_BYTES
from tests.db.seed import Seeder
from tests.db.test_grant_history_reads import plan_nodes
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.engine.test_grant_history import LIMITS, legacy, seed


@pytest.mark.asyncio
async def test_history_page_and_exact_prefix_use_bounded_native_ranges(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 2049))
    async with database.transaction() as writer:
        await writer.exec_driver_sql("ANALYZE attempt_dispatcher")
    nodes: list[JsonObject] = []
    native: list[tuple[object, ...]] = []

    async with database.read_snapshot() as reader:
        with executed_queries(reader) as statements:
            prefix = await read_history_rows(
                reader, scope, LIMITS, PROCESS_BYTES, maximum=1, exact=3000
            )
            rows = await read_history_rows(
                reader,
                scope,
                LIMITS,
                PROCESS_BYTES,
                maximum=129,
                after=3000,
                upper=4500,
            )
        if database.dialect is Dialect.POSTGRESQL:
            plans = [
                as_json_value(
                    (
                        await reader.exec_driver_sql(
                            "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, args
                        )
                    ).scalar_one()
                )
                for sql, args in statements
            ]
            nodes = [node for plan in plans for node in plan_nodes(plan)]
        else:
            plans = [
                str(
                    list(
                        await reader.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args)
                    )
                )
                for sql, args in statements
            ]
            native = await sqlite_instructions(reader, *statements[1])

    assert len(prefix) == 1
    assert prefix[0].epoch == 3000
    assert len(rows) == 129
    assert rows[0].epoch == 3003
    assert rows[-1].epoch == 3387
    if database.dialect is Dialect.POSTGRESQL:
        assert all(node["Node Type"] in ("Limit", "Index Scan") for node in nodes)
        assert all(
            type(node["Actual Rows"]) is int and node["Actual Rows"] <= 129
            for node in nodes
        )
        assert all(node.get("Rows Removed by Filter", 0) == 0 for node in nodes)
        assert all(node.get("Rows Removed by Index Recheck", 0) == 0 for node in nodes)
    else:
        assert all(
            "SEARCH attempt_dispatcher USING INDEX" in str(plan) for plan in plans
        )
        assert all("TEMP B-TREE" not in str(plan) for plan in plans)
        evidence_column = attempt_dispatcher.c.keys().index("evidence")
        reads = [
            row for row in native if row[1] == "Column" and row[3] == evidence_column
        ]
        assert reads
        assert all(row[6] in (0x80, 0xC0) for row in reads)


@pytest.mark.asyncio
async def test_history_authority_lookup_does_not_scan_namespace_nodes(
    database: Database,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        parent = dict(
            (await writer.execute(select(run).where(run.c.id == scope.run_id)))
            .mappings()
            .one()
        )
        original = dict(
            (
                await writer.execute(
                    select(node_instance).where(node_instance.c.id == scope.node_id)
                )
            )
            .mappings()
            .one()
        )
        seeder = Seeder(writer)
        parents: list[dict[str, object]] = []
        nodes: list[dict[str, object]] = []
        for _ in range(2048):
            parent_id = seeder.new_id()
            parents.append({**parent, "id": parent_id, "idempotency_key": parent_id})
            nodes.append(
                {
                    **original,
                    "id": seeder.new_id(),
                    "run_id": parent_id,
                    "current_attempt_id": None,
                }
            )
        await writer.execute(insert(run), parents)
        await writer.execute(insert(node_instance), nodes)
        await writer.exec_driver_sql("ANALYZE run")
        await writer.exec_driver_sql("ANALYZE node_instance")
    plans: list[str] = []
    native: list[JsonObject] = []

    async with database.read_snapshot() as reader:
        with executed_queries(reader) as statements:
            found = await scoped_history_attempt(
                reader, scope.attempt_id, (scope.namespace_id,), LIMITS
            )
        sql, args = statements[0]
        if database.dialect is Dialect.SQLITE:
            plans = [
                str(row)
                for row in await reader.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + sql, args
                )
            ]
        else:
            plan = (
                await reader.exec_driver_sql(
                    "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, args
                )
            ).scalar_one()
            native = plan_nodes(as_json_value(plan))

    assert found is not None
    assert found["node_instance_id"] == scope.node_id
    if database.dialect is Dialect.SQLITE:
        assert not any(
            "SCAN" in value or "LIST SUBQUERY" in value for value in plans
        ), plans
        assert any("CORRELATED SCALAR SUBQUERY" in value for value in plans), plans
    else:
        assert all(
            type(node["Actual Rows"]) is int and node["Actual Rows"] <= 1
            for node in native
        )
        assert all(node.get("Rows Removed by Filter", 0) == 0 for node in native)
