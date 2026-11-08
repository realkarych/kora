from dataclasses import replace
from traceback import format_exception

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationReadFailed,
    GraphPreparationRejected,
    GraphPreparationSettlement,
    GraphPreparationState,
    graph_preparation_parameters,
    graph_preparation_settlement_statement,
    read_graph_preparation_settlement,
)
from kora.db.tables import graph_deployment_preparation
from tests.db.seed import DIGEST, Seeder
from tests.db.test_graph_read_admission import executed_queries


async def seed_preparation(
    connection: AsyncConnection,
    state: GraphPreparationState,
    *,
    bound: bool,
    namespace_name: str = "default",
) -> tuple[GraphPreparationBinding, GraphPreparationSettlement]:
    seeder = Seeder(connection)
    namespace_id = await seeder.namespace(namespace_name)
    graph_id = await seeder.graph(namespace_id, "pipeline")
    version_id = await seeder.graph_version(graph_id)
    binding = GraphPreparationBinding(
        seeder.new_id(), namespace_id, "pipeline", graph_id if bound else None, DIGEST
    )
    outcome = (
        GraphPreparationSettlement(state, graph_id, version_id)
        if state in ("published", "reused")
        else GraphPreparationSettlement(state, None, None)
    )
    await connection.execute(
        graph_deployment_preparation.insert().values(
            id=binding.preparation_id,
            namespace_id=binding.namespace_id,
            graph_name=binding.graph_name,
            initial_graph_id=binding.initial_graph_id,
            deployment_digest=binding.deployment_digest,
            state=outcome.state,
            result_graph_id=outcome.result_graph_id,
            result_graph_version_id=outcome.result_graph_version_id,
        )
    )
    return binding, outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "bound"),
    [
        ("open", False),
        ("closed_without_publication", True),
        ("published", False),
        ("reused", True),
    ],
)
async def test_preparation_reads_exact_original_settlement(
    database: Database, state: GraphPreparationState, bound: bool
) -> None:
    async with database.transaction() as connection:
        binding, expected = await seed_preparation(connection, state, bound=bound)

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            actual = await read_graph_preparation_settlement(connection, binding)
        plan: list[tuple[object, ...]] = []
        if database.dialect is Dialect.SQLITE:
            [(statement, arguments)] = queries
            rows = await connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + statement, arguments
            )
            plan = [tuple(row) for row in rows]

    assert actual == expected
    assert actual is not None
    assert state not in repr(actual)
    assert DIGEST not in repr(binding)
    assert binding.preparation_id not in repr(binding)
    if expected.result_graph_id is not None:
        assert expected.result_graph_id not in repr(actual)
    [(statement, _)] = queries
    assert "FROM graph_deployment_preparation" in statement
    assert "JOIN" not in statement
    if database.dialect is Dialect.SQLITE:
        assert len(plan) == 1
        assert "SEARCH graph_deployment_preparation USING INDEX" in str(plan[0][3])
        assert "(id=?)" in str(plan[0][3])


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_preparation_does_not_return_foreign_binding(
    database: Database, bound: bool
) -> None:
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "published", bound=bound)
        seeder = Seeder(connection)
        foreign_namespace = await seeder.namespace("other")
        foreign_graph = await seeder.graph(foreign_namespace, binding.graph_name)
        absent_preparation = seeder.new_id()
    mismatches = (
        replace(binding, preparation_id=absent_preparation),
        replace(binding, namespace_id=foreign_namespace),
        replace(binding, graph_name="other_graph"),
        replace(binding, deployment_digest="a" * 64),
        replace(binding, initial_graph_id=foreign_graph),
    )
    if bound:
        mismatches = (*mismatches, replace(binding, initial_graph_id=None))

    async with database.read_snapshot() as connection:
        outcomes = [
            await read_graph_preparation_settlement(connection, mismatched)
            for mismatched in mismatches
        ]

    assert outcomes == [None] * len(mismatches)


@pytest.mark.asyncio
async def test_invalid_preparation_binding_refuses_before_sql(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
    invalid = (
        replace(binding, preparation_id="Z" * 26),
        replace(binding, namespace_id="id"),
        replace(binding, graph_name="bad\0name"),
        replace(binding, initial_graph_id="lower_case"),
        replace(binding, deployment_digest="a" * 65),
    )

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            for candidate in invalid:
                with pytest.raises(
                    GraphPreparationRejected,
                    match="^graph preparation settlement rejected$",
                ):
                    await read_graph_preparation_settlement(connection, candidate)

    assert queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        {"state": "other"},
        {"result_graph_id": "A" * 8192},
        {"result_graph_id": "0" * 25 + "I"},
        {"result_graph_id": "0" * 26},
        {"result_graph_version_id": None},
    ],
)
async def test_preparation_projects_no_values_for_malformed_settlement(
    database: Database, corruption: dict[str, str | None]
) -> None:
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "published", bound=True)
        sqlite = database.dialect is Dialect.SQLITE
        if sqlite:
            await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_deployment_preparation "
                "DROP CONSTRAINT ck_graph_deployment_preparation_result_shape, "
                "DROP CONSTRAINT ck_graph_deployment_preparation_result_target, "
                "DROP CONSTRAINT ck_graph_deployment_preparation_result_graph_id_domain"
            )
        try:
            await connection.execute(
                graph_deployment_preparation.update()
                .where(graph_deployment_preparation.c.id == binding.preparation_id)
                .values(**corruption)
            )
        finally:
            if sqlite:
                await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        statement = graph_preparation_settlement_statement(connection.dialect.name)

        projection = (
            await connection.execute(statement, graph_preparation_parameters(binding))
        ).one()
        with pytest.raises(
            GraphPreparationRejected, match="^graph preparation settlement rejected$"
        ):
            await read_graph_preparation_settlement(connection, binding)

    assert tuple(projection) == (None, None, None)


@pytest.mark.asyncio
async def test_preparation_refuses_statement_failure_without_private_error_text(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
        await connection.exec_driver_sql(
            "ALTER TABLE graph_deployment_preparation RENAME TO unqueryable_preparation"
        )
    caller_error = ValueError(binding.deployment_digest)

    try:
        raise caller_error
    except ValueError:
        with pytest.raises(
            GraphPreparationReadFailed,
            match="^graph preparation settlement read failed$",
        ) as refused:
            async with database.read_snapshot() as connection:
                await read_graph_preparation_settlement(connection, binding)

    error = refused.value
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert any(error.__context__ is context for context in (None, caller_error))
    rendered = "".join(format_exception(error))
    assert binding.deployment_digest not in rendered
    assert binding.preparation_id not in rendered
