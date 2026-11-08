import asyncio
from dataclasses import replace
from traceback import format_exception

import pytest
from sqlalchemy import event, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import Database
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.graph_publication_ordinal_v1 import (
    ORDINAL_INVALID_INDEX,
    ORDINAL_VALID_INDEX,
)
from kora.db.tables import graph_version
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    GraphPublicationOrdinalReadFailed,
    GraphPublicationOrdinalUnavailable,
    LockError,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, lock_inventory
from tests.engine.test_graph_publication_target import (
    publication_locks,
    publication_scopes,
    seed_target,
)


async def read_ordinal(
    database: Database, binding: GraphPreparationBinding, selected: str
) -> int | None:
    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        assert (
            await locks.read_graph_publication_target(writer, binding, selected)
            == selected
        )
        return await locks.read_graph_publication_ordinal(writer, binding, selected)


@pytest.mark.asyncio
async def test_publication_ordinal_requires_exact_live_observed_target(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.graph_version(selected, 1)
        legacy = await seed.graph_version(selected, 9)
        await writer.execute(
            graph_version.update()
            .where(graph_version.c.id == legacy)
            .values(status="retired")
        )
        another_namespace = await seed.namespace("other")
        other = await seed.graph(another_namespace)
        await seed.graph_version(other, 8000)
        empty = await seed.graph(binding.namespace_id, "empty")
    bound = replace(binding, initial_graph_id=selected)
    different = replace(bound, deployment_digest="a" * 64)
    foreign_namespace = replace(binding, namespace_id=another_namespace)
    empty_binding = replace(binding, graph_name="empty")

    async with database.transaction() as writer:
        locks = await publication_locks(writer, bound, selected)
        with executed_queries(writer) as before:
            with pytest.raises(LockError, match="target was not observed"):
                await locks.read_graph_publication_ordinal(writer, bound, selected)
        assert (
            await locks.read_graph_publication_target(writer, bound, selected)
            == selected
        )
        with executed_queries(writer) as denied:
            with pytest.raises(LockError, match="target was not observed"):
                await locks.read_graph_publication_ordinal(writer, different, selected)
            with pytest.raises(LockError, match="required transaction locks"):
                await locks.read_graph_publication_ordinal(
                    writer, foreign_namespace, selected
                )
            with pytest.raises(LockError, match="differs from the bound graph"):
                await locks.read_graph_publication_ordinal(writer, bound, empty)
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.read_graph_publication_ordinal(writer, bound, selected)
                )
        async with database.read_transaction() as reader:
            with executed_queries(reader) as wrong_connection:
                with pytest.raises(LockError, match="does not own"):
                    await locks.read_graph_publication_ordinal(reader, bound, selected)
        with executed_queries(writer) as hit_sql:
            highest = await locks.read_graph_publication_ordinal(
                writer, bound, selected
            )
        await locks.acquire_existing_storage_addresses((), max_addresses=0)
        with executed_queries(writer) as late:
            with pytest.raises(LockError, match="precedes storage addresses"):
                await locks.read_graph_publication_ordinal(writer, bound, selected)
    async with database.transaction() as writer:
        with executed_queries(writer) as stale:
            with pytest.raises(WriterTransactionError, match="not current"):
                await locks.read_graph_publication_ordinal(writer, bound, selected)
    absent = await read_ordinal(database, empty_binding, empty)

    assert before == denied == wrong_connection == late == stale == []
    assert highest == 9
    assert absent is None
    [(statement, parameters)] = hit_sql
    assert selected in parameters
    assert all(
        field not in statement
        for field in ("execution_contract", "resources", "deployed_at")
    )
    assert database.writer_is_free()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_number", [-1, 0])
async def test_publication_ordinal_refuses_invalid_low_number(
    database: Database, invalid_number: int
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.graph_version(selected, 3)
        await seed.graph_version(selected, invalid_number)

    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        assert (
            await locks.read_graph_publication_target(writer, binding, selected)
            == selected
        )
        with pytest.raises(GraphPublicationOrdinalUnavailable) as refusal:
            await locks.read_graph_publication_ordinal(writer, binding, selected)
        with pytest.raises(LockError, match="cannot be used"):
            locks.require(None, publication_scopes(binding, selected))

    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert await lock_inventory(database) == []
    assert database.writer_is_free()


def assert_postgresql_endpoint_plan(rows: list[str]) -> None:
    assert sum("Limit" in row for row in rows) == 2
    scans = [row for row in rows if "Index Only Scan" in row or "Index Scan" in row]
    assert len(scans) == 2
    assert sum("Backward" in row for row in scans) == 1
    assert all("uq_graph_version_graph_id_number" in row for row in scans)
    conditions = [row for row in rows if "Index Cond:" in row]
    assert len(conditions) == 2
    assert all("graph_id" in condition for condition in conditions)
    assert not any(
        "Seq Scan" in row or "Sort" in row or "Bitmap" in row for row in rows
    )


async def assert_native_plans(
    writer: AsyncConnection,
    selected: str,
    empty: str,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    if writer.dialect.name == "sqlite":
        plan = (
            await writer.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
        ).all()
        rendered = str(plan)
        assert ORDINAL_VALID_INDEX in rendered
        assert ORDINAL_INVALID_INDEX in rendered
        assert rendered.count("(graph_id=?)") == 2
        assert "SCAN main.graph_version" not in rendered
        return
    for arguments in (parameters, (empty,)):
        ordinary = (
            await writer.exec_driver_sql(
                "EXPLAIN (ANALYZE, COSTS OFF) " + statement, arguments
            )
        ).scalars()
        assert_postgresql_endpoint_plan(list(ordinary))
    await writer.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
    await writer.exec_driver_sql("PREPARE kora_ordinal_probe AS " + statement)
    try:
        for graph_id in (selected, empty):
            argument = str(
                literal(graph_id).compile(
                    dialect=writer.dialect, compile_kwargs={"literal_binds": True}
                )
            )
            plan = (
                await writer.exec_driver_sql(
                    "EXPLAIN (ANALYZE, COSTS OFF) "
                    f"EXECUTE kora_ordinal_probe({argument})"
                )
            ).scalars()
            assert_postgresql_endpoint_plan(list(plan))
    finally:
        await writer.exec_driver_sql("DEALLOCATE kora_ordinal_probe")


@pytest.mark.asyncio
async def test_publication_ordinal_native_plans_use_exact_graph_prefix(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        seed_id = await seed.graph_version(selected, 5)
        empty = await seed.graph(binding.namespace_id, "empty")
        original = (
            (
                await writer.execute(
                    select(graph_version).where(graph_version.c.id == seed_id)
                )
            )
            .mappings()
            .one()
        )
        graph_ids = [
            await seed.graph(binding.namespace_id, f"mixed-{index}")
            for index in range(64)
        ]
        await writer.execute(
            graph_version.insert(),
            [
                dict(original)
                | {"id": seed.new_id(), "graph_id": graph_id, "number": ordinal}
                for graph_id in graph_ids
                for ordinal in range(1, 17)
            ],
        )
        await writer.exec_driver_sql("ANALYZE graph_version")

    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        assert (
            await locks.read_graph_publication_target(writer, binding, selected)
            == selected
        )
        with executed_queries(writer) as native:
            observed = await locks.read_graph_publication_ordinal(
                writer, binding, selected
            )
        [(statement, parameters)] = native
        await assert_native_plans(writer, selected, empty, statement, parameters)

    assert observed == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["failed", "cancelled"])
async def test_publication_ordinal_native_failure_releases_resources(
    database: Database, cancel: bool
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        await Seeder(writer).graph_version(selected, 7)
    ledger = ConnectionLedger(database)
    reading = asyncio.Event()
    unfinished = asyncio.Event()

    def pause(
        connection: object, cursor: object, statement: str, *args: object
    ) -> None:
        if "ORDER BY number" in statement:
            reading.set()
            await_(asyncio.wait_for(unfinished.wait(), DEADLINE))

    async def interrupted() -> None:
        async with database.transaction() as writer:
            locks = await publication_locks(writer, binding, selected)
            assert (
                await locks.read_graph_publication_target(writer, binding, selected)
                == selected
            )
            if cancel:
                event.listen(writer.sync_connection, "before_cursor_execute", pause)
            else:
                await writer.exec_driver_sql(
                    "ALTER TABLE graph_version RENAME TO unavailable_graph_version"
                )
            try:
                await locks.read_graph_publication_ordinal(writer, binding, selected)
            except (asyncio.CancelledError, GraphPublicationOrdinalReadFailed):
                with pytest.raises(
                    (LockError, WriterTransactionError),
                    match="cannot be used|not current",
                ):
                    locks.require(None, publication_scopes(binding, selected))
                raise
            finally:
                if cancel:
                    event.remove(writer.sync_connection, "before_cursor_execute", pause)

    if cancel:
        task = asyncio.create_task(interrupted())
        try:
            await asyncio.wait_for(reading.wait(), DEADLINE)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, DEADLINE)
    else:
        with pytest.raises(GraphPublicationOrdinalReadFailed) as failure:
            await interrupted()
        assert failure.value.__cause__ is failure.value.__context__ is None
        assert "SELECT" not in "".join(format_exception(failure.value))
    recovered = await read_ordinal(database, binding, selected)

    assert recovered == 7
    assert await lock_inventory(database) == []
    assert database.writer_is_free()
    assert ledger.outstanding == database.units_in_flight() == 0
