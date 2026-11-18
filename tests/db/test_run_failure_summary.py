from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from kora.db.connection import Database, Dialect
from kora.db.migrations import upgrade_database
from kora.db.run_failure_summary import (
    FailedNode,
    RunFailureSummary,
    RunFailureSummaryUnavailable,
    failure_summary_statement,
    read_run_failure_summary,
)
from kora.db.tables import attempt, node_instance
from kora.ir import ErrorClass
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.support.databases import encoded_sqlite_settings


async def summary_run(database: Database) -> tuple[str, str]:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace(name=f"summary-{seed.new_id()}")
        graph = await seed.graph(namespace)
        version = await seed.graph_version(graph)
        run_id = await seed.run(namespace, graph, version)
    return namespace, run_id


@pytest.mark.asyncio
async def test_run_failure_summary_is_bounded_and_deterministic(
    database: Database,
) -> None:
    namespace, run_id = await summary_run(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.nodes(
            run_id, [f"node_{i:03}" for i in reversed(range(29))], "failed", True
        )
        await seed.node(run_id, "a_upstream", "upstream_failed")
        await seed.node(run_id, "a_cancelled", "cancelled")
        await seed.node(run_id, "body/failed", "failed", False)
        await seed.node(run_id, "ignored", "skipped")

    async with database.read_transaction() as reader:
        with executed_queries(reader) as statements:
            summary = await read_run_failure_summary(reader, namespace, run_id)

    assert summary == RunFailureSummary(
        31, tuple(FailedNode(f"node_{i:03}", "failed", None) for i in range(20))
    )
    assert len(statements) == 1
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_failure_summary_uses_latest_applied_attempt_not_current_pointer(
    database: Database,
) -> None:
    namespace, run_id = await summary_run(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        node = await seed.node(run_id, "failed_node", "failed")
        older = await seed.attempt(node, "failed", 1)
        newer = await seed.attempt(node, "succeeded", 2)
        cancelled = await seed.node(run_id, "cancelled_node", "cancelled")
        applied = await seed.attempt(cancelled, "failed")
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node)
            .values(current_attempt_id=older)
        )
        await writer.execute(
            update(attempt).where(attempt.c.id == older).values(error_class="timeout")
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == newer)
            .values(error_class="private-unused-class", error={"private": "retained"})
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == applied)
            .values(error_class="command_failed", error={"private": "retained"})
        )
        await seed.node(run_id, "a_upstream", "upstream_failed")

    async with database.read_transaction() as reader:
        summary = await read_run_failure_summary(reader, namespace, run_id)

    assert summary == RunFailureSummary(
        3,
        (
            FailedNode("failed_node", "failed", None),
            FailedNode("a_upstream", "upstream_failed", None),
            FailedNode("cancelled_node", "cancelled", ErrorClass.COMMAND_FAILED),
        ),
    )


@pytest.mark.asyncio
async def test_failure_summary_distinguishes_empty_foreign_and_absent_runs(
    database: Database,
) -> None:
    namespace, run_id = await summary_run(database)
    foreign_namespace, foreign_run = await summary_run(database)
    async with database.transaction() as writer:
        await Seeder(writer).node(foreign_run, "foreign", "failed")

    async with database.read_transaction() as reader:
        empty = await read_run_failure_summary(reader, namespace, run_id)
        foreign = await read_run_failure_summary(reader, foreign_namespace, run_id)
        absent = await read_run_failure_summary(reader, namespace, namespace)

    assert empty == RunFailureSummary(0, ())
    assert foreign is absent is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["az" + "x" * 63, "az\n", "az ", "az.bad", "az\u0100", "AZ"]
)
async def test_selected_invalid_path_refuses_the_whole_failure_summary(
    database: Database, path: str
) -> None:
    namespace, run_id = await summary_run(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.nodes(run_id, [f"aa{i:02}" for i in range(19)], "failed", True)
        await seed.node(run_id, path, "failed")
        await seed.node(run_id, "zz_valid_replacement", "failed")

    async with database.read_transaction() as reader:
        with pytest.raises(RunFailureSummaryUnavailable) as caught:
            await read_run_failure_summary(reader, namespace, run_id)
        reusable = await reader.scalar(select(1))

    assert str(caught.value) == "run failure summary is unavailable"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert reusable == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_class"),
    [("private-invalid", None), ("failed", None), ("failed", "private-invalid")],
)
async def test_failure_summary_refuses_invalid_latest_applied_status_or_class(
    database: Database, status: str, error_class: str | None
) -> None:
    namespace, run_id = await summary_run(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        node = await seed.node(run_id, "failed_node", "failed")
        physical = await seed.attempt(node, status)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == physical)
            .values(error_class=error_class)
        )

    async with database.read_transaction() as reader:
        with pytest.raises(RunFailureSummaryUnavailable) as caught:
            await read_run_failure_summary(reader, namespace, run_id)

    assert str(caught.value) == "run failure summary is unavailable"
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_failure_summary_bounds_original_bytes_before_driver_decoding(
    database: Database,
) -> None:
    namespace, run_id = await summary_run(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.node(run_id, "a" * 1024, "failed")

    async with database.read_transaction() as reader:
        with executed_queries(reader) as statements:
            with await reader.execute(
                failure_summary_statement(reader, namespace, run_id)
            ) as result:
                rows = [tuple(row) for row in result]
        with pytest.raises(RunFailureSummaryUnavailable):
            await read_run_failure_summary(reader, namespace, run_id)

    assert len(statements) == 1
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][2] is None
    query = statements[0][0]
    assert all(
        field not in query
        for field in (
            "attempt.outcome",
            "attempt.recovery",
            "attempt.scratch",
            "attempt.error ",
        )
    )


@pytest.mark.asyncio
async def test_failure_summary_observes_only_its_owned_top_level_domain(
    database: Database, tmp_path: Path
) -> None:
    namespace, run_id = await summary_run(database)
    _, foreign_run = await summary_run(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        selected = await seed.nodes(
            run_id, [f"node_{i:03}" for i in range(30)], "failed", True
        )
        await seed.nodes(run_id, [f"body/{i}" for i in range(100)], "failed", False)
        await seed.nodes(
            foreign_run, [f"foreign_{i}" for i in range(1000)], "failed", True
        )
        for node in selected[:20]:
            for number in range(1, 4):
                physical = await seed.attempt(node, "failed", number)
                await writer.execute(
                    update(attempt)
                    .where(attempt.c.id == physical)
                    .values(error_class="timeout")
                )
        await writer.execute(text("ANALYZE"))
    prefix = (
        "EXPLAIN QUERY PLAN "
        if database.dialect is Dialect.SQLITE
        else "EXPLAIN (ANALYZE, BUFFERS) "
    )

    async with database.read_transaction() as reader:
        with executed_queries(reader) as statements:
            summary = await read_run_failure_summary(reader, namespace, run_id)
        [(query, parameters)] = statements
        with await reader.exec_driver_sql(prefix + query, parameters) as result:
            plan = "\n".join(str(tuple(row)) for row in result)
    (tmp_path / "failure-summary-native-plan.txt").write_text(plan)
    (tmp_path / "failure-summary-query.sql").write_text(query)

    assert summary == RunFailureSummary(
        30,
        tuple(
            FailedNode(f"node_{i:03}", "failed", ErrorClass.TIMEOUT) for i in range(20)
        ),
    )
    assert namespace in parameters
    assert run_id in parameters
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
@pytest.mark.parametrize("kind", ["malformed", "blob", "nul"])
async def test_selected_native_sqlite_path_is_strictly_admitted(
    tmp_path: Path, encoding: str, kind: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "native.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    raw = {"UTF-8": b"\xff", "UTF-16le": b"\x00\xd8", "UTF-16be": b"\xd8\x00"}[encoding]
    try:
        namespace, run_id = await summary_run(database)
        async with database.transaction() as writer:
            node = await Seeder(writer).node(run_id, "a", "failed")
            if kind != "nul":
                statement = (
                    text(
                        "UPDATE node_instance SET node_path = CAST(:raw AS TEXT) "
                        "WHERE id = :id"
                    )
                    if kind == "malformed"
                    else text(
                        "UPDATE node_instance SET node_path = :raw WHERE id = :id"
                    )
                )
                await writer.execute(statement, {"raw": raw, "id": node})
            else:
                await writer.execute(
                    text("UPDATE node_instance SET node_path = :path WHERE id = :id"),
                    {"path": "a\x00", "id": node},
                )

        async with database.read_transaction() as reader:
            with pytest.raises(RunFailureSummaryUnavailable) as caught:
                await read_run_failure_summary(reader, namespace, run_id)

        assert str(caught.value) == "run failure summary is unavailable"
        assert caught.value.__context__ is caught.value.__cause__ is None
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
@pytest.mark.parametrize(
    ("valid", "unselected"),
    [("az" + "x" * 62, "a\u0100"), ("az", "az "), ("az", "az\x00")],
)
async def test_failure_summary_selects_by_code_point_before_limit_in_each_encoding(
    tmp_path: Path, encoding: str, valid: str, unselected: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "summary.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    ledger = ConnectionLedger(database)
    try:
        namespace, run_id = await summary_run(database)
        async with database.transaction() as writer:
            seed = Seeder(writer)
            await seed.nodes(run_id, [f"aa{i:02}" for i in range(19)], "failed", True)
            native_node = await seed.node(run_id, "unselected_native", "failed")
            await writer.execute(
                text("UPDATE node_instance SET node_path = :path WHERE id = :id"),
                {"path": unselected, "id": native_node},
            )
            await seed.node(run_id, valid, "failed")

        async with database.read_transaction() as reader:
            summary = await read_run_failure_summary(reader, namespace, run_id)

        assert summary is not None
        assert summary.count == 21
        assert [item.node_path for item in summary.sample] == [
            *(f"aa{i:02}" for i in range(19)),
            valid,
        ]
        assert database.units_in_flight() == ledger.outstanding == 0
    finally:
        await database.close()
    assert ledger.opened == ledger.closed > 0
