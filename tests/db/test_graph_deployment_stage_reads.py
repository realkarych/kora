from dataclasses import replace
from pathlib import Path
from traceback import format_exception

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.graph_deployment_stage_reads import (
    GraphStage,
    GraphStageInventory,
    GraphStageInventoryReadFailed,
    GraphStageInventoryRejected,
    GraphStageOccurrence,
    GraphStageScope,
    read_graph_stage_inventory,
)
from kora.db.migrations import upgrade_database
from kora.db.tables import graph_deployment_stage as stage
from kora.db.tables import graph_deployment_stage_occurrence as occurrence
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import (
    ADDRESS_DIGEST,
    PreparedScope,
    occurrence_values,
    prepare_graph,
    stage_values,
)
from tests.db.test_graph_read_admission import executed_queries
from tests.support.databases import encoded_sqlite_settings


def inventory_scope(scope: PreparedScope) -> GraphStageScope:
    return GraphStageScope(scope.preparation_id, scope.namespace_id)


def native_limits(
    queries: list[tuple[str, tuple[object, ...]]], dialect: Dialect
) -> list[object]:
    assert all("LIMIT" in statement for statement, _ in queries)
    position = -2 if dialect is Dialect.SQLITE else -1
    return [args[position] for _, args in queries]


async def replay(
    connection: AsyncConnection, query: tuple[str, tuple[object, ...]]
) -> list[tuple[object, ...]]:
    statement, args = query
    return [tuple(row) for row in await connection.exec_driver_sql(statement, args)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cohort", ["empty", "sparse", "max"])
async def test_stage_inventory_reads_exact_scope_in_ordinal_order(
    database: Database, cohort: str
) -> None:
    async with database.transaction() as connection:
        prepared = await prepare_graph(connection)
        scope = inventory_scope(prepared)
        seeder = Seeder(connection)
        ordinals = {"empty": (), "sparse": (3, 63), "max": tuple(range(64))}[cohort]
        stage_count = {"empty": 0, "sparse": 1, "max": 64}[cohort]
        identities = [seeder.new_id() for _ in range(stage_count)]
        stages = [
            GraphStage(
                stage_id,
                scope.namespace_id,
                scope.preparation_id,
                f"Store-{index}",
                ADDRESS_DIGEST,
                index + 1,
                0,
            )
            for index, stage_id in enumerate(identities)
        ]
        bindings = [
            GraphStageOccurrence(
                identities[index if cohort == "max" else 0],
                scope.namespace_id,
                scope.preparation_id,
                ordinal,
            )
            for index, ordinal in enumerate(ordinals)
        ]
        if stages:
            await connection.execute(
                stage.insert(),
                [
                    {
                        **stage_values(prepared, item.stage_id, item.fence_incarnation),
                        "store_id": item.store_id,
                    }
                    for item in stages
                ],
            )
            await connection.execute(
                occurrence.insert(),
                [
                    occurrence_values(prepared, item.stage_id, item.ordinal)
                    for item in bindings
                ],
            )

    async with database.read_snapshot() as connection:
        transaction = connection.get_transaction()
        with executed_queries(connection) as queries:
            actual = await read_graph_stage_inventory(connection, scope)
        borrowed = connection.get_transaction()

    assert transaction is not None
    assert borrowed is transaction
    assert actual == GraphStageInventory(tuple(stages), tuple(bindings))
    assert scope.preparation_id not in repr(scope)
    assert scope.namespace_id not in repr(actual)
    assert ADDRESS_DIGEST not in repr(actual.stages)
    assert len(queries) == 2 + stage_count
    assert native_limits(queries, database.dialect) == [
        65,
        65,
        *range(65, 65 - stage_count, -1),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem", ["namespace", "preparation", "foreign_stage", "orphan"]
)
async def test_stage_inventory_refuses_both_scope_conflicts_and_orphans(
    database: Database, problem: str
) -> None:
    async with database.transaction() as connection:
        first = await prepare_graph(connection)
        other = await prepare_graph(connection, "other")
        seeder = Seeder(connection)
        local, foreign = seeder.new_id(), seeder.new_id()
        await connection.execute(
            stage.insert(), [stage_values(first, local), stage_values(other, foreign)]
        )
        if problem != "orphan":
            bound = occurrence_values(first, local, 0)
            if problem == "namespace":
                bound["namespace_id"] = other.namespace_id
            await connection.execute(occurrence.insert().values(**bound))
        if problem == "preparation":
            await connection.execute(
                occurrence.insert().values(**occurrence_values(other, local, 1))
            )
        if problem == "foreign_stage":
            await connection.execute(
                occurrence.insert().values(**occurrence_values(first, foreign, 1))
            )

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            with pytest.raises(GraphStageInventoryRejected):
                await read_graph_stage_inventory(connection, inventory_scope(first))
        projected = await replay(connection, queries[-1])

    assert len(queries) == (3 if problem == "preparation" else 2)
    if problem in ("preparation", "namespace"):
        assert (None,) * 4 in projected


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", ["stages", "late_occurrences"])
async def test_stage_inventory_masks_overflow_in_native_ranges(
    database: Database, overflow: str
) -> None:
    async with database.transaction() as connection:
        first = await prepare_graph(connection)
        other = await prepare_graph(connection, "other")
        seeder = Seeder(connection)
        count = 65 if overflow == "stages" else 2
        identities = [seeder.new_id() for _ in range(count)]
        await connection.execute(
            stage.insert(),
            [
                {**stage_values(first, key), "store_id": f"Store-{index}"}
                for index, key in enumerate(identities)
            ],
        )
        if overflow == "late_occurrences":
            await connection.execute(
                occurrence.insert(),
                [
                    occurrence_values(first, identities[0], ordinal)
                    for ordinal in range(63)
                ]
                + [
                    occurrence_values(first, identities[1], 63),
                    occurrence_values(other, identities[1], 0),
                ],
            )

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            with pytest.raises(GraphStageInventoryRejected):
                await read_graph_stage_inventory(connection, inventory_scope(first))
        projected = [await replay(connection, query) for query in queries]

    if overflow == "stages":
        assert native_limits(queries, database.dialect) == [65]
        assert projected == [[(None,) * 7] * 65]
    else:
        assert native_limits(queries, database.dialect) == [65, 65, 65, 2]
        assert [len(rows) for rows in projected] == [2, 64, 63, 2]
        assert projected[-1] == [(None,) * 4] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["stage", "occurrence"])
async def test_stage_inventory_masks_malformed_native_rows(
    database: Database, malformed: str
) -> None:
    async with database.transaction() as connection:
        prepared = await prepare_graph(connection)
        stage_id = Seeder(connection).new_id()
        await connection.execute(
            stage.insert().values(**stage_values(prepared, stage_id))
        )
        await connection.execute(
            occurrence.insert().values(**occurrence_values(prepared, stage_id, 0))
        )
        if database.dialect is Dialect.SQLITE:
            await connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        elif malformed == "stage":
            await connection.exec_driver_sql(
                "ALTER TABLE graph_deployment_stage DROP CONSTRAINT "
                "ck_graph_deployment_stage_store_id_domain"
            )
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_deployment_stage_occurrence DROP CONSTRAINT "
                "ck_graph_deployment_stage_occurrence_ordinal_range"
            )
        try:
            if malformed == "stage":
                await connection.execute(stage.update().values(store_id="x" * 8192))
            else:
                await connection.execute(occurrence.update().values(ordinal=64))
        finally:
            if database.dialect is Dialect.SQLITE:
                await connection.exec_driver_sql(
                    "PRAGMA ignore_check_constraints = OFF"
                )

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            with pytest.raises(GraphStageInventoryRejected):
                await read_graph_stage_inventory(connection, inventory_scope(prepared))
        raw = await replay(connection, queries[-1])

    assert raw == [(None,) * (7 if malformed == "stage" else 4)]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
async def test_stage_inventory_applies_native_sqlite_text_widths(
    tmp_path: Path, encoding: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "utf16.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    try:
        async with database.transaction() as connection:
            scope = await prepare_graph(connection)
            stage_id = Seeder(connection).new_id()
            longest_store = "x" * 256
            await connection.execute(
                stage.insert().values(
                    **{**stage_values(scope, stage_id), "store_id": longest_store}
                )
            )
            await connection.execute(
                occurrence.insert().values(**occurrence_values(scope, stage_id, 63))
            )

        async with database.read_snapshot() as connection:
            actual_encoding = await connection.exec_driver_sql("PRAGMA encoding")
            stored_encoding = actual_encoding.scalar_one()
            actual = await read_graph_stage_inventory(
                connection, inventory_scope(scope)
            )
    finally:
        await database.close()

    assert stored_encoding == encoding
    assert len(actual.stages) == len(actual.occurrences) == 1
    assert actual.stages[0].store_id == longest_store
    assert actual.occurrences[0].ordinal == 63


@pytest.mark.asyncio
async def test_stage_inventory_refuses_statement_failure_without_private_error_text(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        prepared = await prepare_graph(connection)
        await connection.exec_driver_sql(
            "ALTER TABLE graph_deployment_stage RENAME TO unqueryable_deployment_stage"
        )
    scope = inventory_scope(prepared)
    caller_error = ValueError(scope.preparation_id)

    try:
        raise caller_error
    except ValueError:
        with pytest.raises(
            GraphStageInventoryReadFailed,
            match="^graph deployment stage inventory read failed$",
        ) as refused:
            async with database.read_snapshot() as connection:
                await read_graph_stage_inventory(connection, scope)

    error = refused.value
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert any(error.__context__ is context for context in (None, caller_error))
    rendered = "".join(format_exception(error))
    assert scope.preparation_id not in rendered
    assert scope.namespace_id not in rendered
    assert "unqueryable_deployment_stage" not in rendered


@pytest.mark.asyncio
async def test_stage_inventory_rejects_invalid_scope_before_sql(
    database: Database,
) -> None:
    valid_id = "0" * 26
    scopes = (GraphStageScope("bad", valid_id), GraphStageScope(valid_id, "bad"))

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            for scope in scopes:
                with pytest.raises(GraphStageInventoryRejected):
                    await read_graph_stage_inventory(connection, scope)

    assert queries == []


def assert_indexed_range(
    plan: list[tuple[object, ...]], dialect: Dialect, name: str, prefix: str
) -> None:
    if dialect is Dialect.SQLITE:
        searches = [
            row for row in plan if name in str(row[3]) and "SEARCH " in str(row[3])
        ]
        [search] = searches
        assert f"{prefix}=?" in str(search[3])
        assert not any(
            row[1] == search[1] and "USE TEMP B-TREE" in str(row[3]) for row in plan
        )
        return
    details = [str(row[0]) for row in plan]
    limit = next(index for index, line in enumerate(details) if "->  Limit" in line)
    inner = "\n".join(details[limit + 1 :])
    assert f"using {name}" in inner
    assert f"Index Cond: ({prefix} =" in inner
    assert "Sort" not in inner
    assert "Seq Scan" not in inner


@pytest.mark.asyncio
async def test_stage_inventory_indexes_selective_hits_and_misses(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        selected = await prepare_graph(connection)
        unrelated = await prepare_graph(connection, "unrelated")
        seeder = Seeder(connection)
        target = seeder.new_id()
        missing = seeder.new_id()
        scope = inventory_scope(selected)
        history = [(seeder.new_id(), seeder.new_id()) for _ in range(1024)]
        await connection.execute(stage.insert(), [stage_values(selected, target)])
        await connection.execute(
            stage.insert(),
            [
                {**stage_values(unrelated, key), "store_id": f"Other-{index}"}
                for index, (key, _) in enumerate(history)
            ],
        )
        await connection.execute(
            occurrence.insert(),
            [
                occurrence_values(selected, target, 0),
                *(
                    {
                        **occurrence_values(unrelated, key, 0),
                        "preparation_id": preparation_id,
                    }
                    for key, preparation_id in history
                ),
            ],
        )
        await connection.exec_driver_sql("ANALYZE graph_deployment_stage")
        await connection.exec_driver_sql("ANALYZE graph_deployment_stage_occurrence")

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as hits:
            inventory = await read_graph_stage_inventory(connection, scope)
        with executed_queries(connection) as misses:
            absent = await read_graph_stage_inventory(
                connection, replace(scope, preparation_id=missing)
            )
        query, args = hits[-1]
        by_stage_miss = query, tuple(missing if arg == target else arg for arg in args)
        actual_miss = await replay(connection, by_stage_miss)
        prefix = (
            "EXPLAIN QUERY PLAN "
            if database.dialect is Dialect.SQLITE
            else "EXPLAIN (COSTS OFF) "
        )
        plans = [
            await replay(connection, (prefix + sql, arguments))
            for sql, arguments in (*hits, *misses, by_stage_miss)
        ]

    assert len(inventory.stages) == len(inventory.occurrences) == 1
    assert absent == GraphStageInventory((), ())
    assert actual_miss == []
    assert len(plans) == 6
    occurrence_primary = (
        "sqlite_autoindex_graph_deployment_stage_occurrence_1"
        if database.dialect is Dialect.SQLITE
        else "pk_graph_deployment_stage_occurrence"
    )
    for plan, (index, prefix_column) in zip(
        plans,
        (
            ("ix_graph_deployment_stage_preparation", "preparation_id"),
            (occurrence_primary, "preparation_id"),
            ("ix_graph_deployment_stage_occurrence_stage", "stage_id"),
        )
        * 2,
        strict=True,
    ):
        assert_indexed_range(plan, database.dialect, index, prefix_column)
