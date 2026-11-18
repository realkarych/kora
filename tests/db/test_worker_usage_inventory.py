from decimal import Decimal

import pytest
from sqlalchemy import event, insert, literal, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.cost_reads import CostReadRefused
from kora.db.tables import attempt, usage_record
from kora.db.worker_usage import read_attempt_usage
from tests.db.seed import Seeder
from tests.ir.test_cost_models import usage_payload


async def usage_scope(seed: Seeder) -> tuple[str, str]:
    namespace_id = await seed.namespace()
    graph_id = await seed.graph(namespace_id)
    version_id = await seed.graph_version(graph_id)
    run_id = await seed.run(namespace_id, graph_id, version_id)
    node_id = await seed.node(run_id, "usage")
    return namespace_id, await seed.attempt(node_id)


async def save_usage(
    connection: AsyncConnection,
    seed: Seeder,
    attempt_id: str,
    model: str,
    **changes: object,
) -> str:
    usage_id = seed.new_id()
    values = usage_payload(model=model, source={"nul\0": [1, 1.0, -0.0]})
    values.update(
        id=usage_id,
        attempt_id=attempt_id,
        normalization_version=1,
        dispatcher_id=seed.new_id(),
        usd_estimate=Decimal("2"),
        same_basis_usd_floor=Decimal("2"),
        **seed.stamps(),
    )
    values.update(changes)
    await connection.execute(insert(usage_record).values(**values))
    return usage_id


@pytest.mark.asyncio
async def test_usage_inventory_is_attempt_scoped_and_preserves_private_floor(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id, attempt_id = await usage_scope(seed)
        node_id = (
            await connection.execute(
                select(attempt.c.node_instance_id).where(attempt.c.id == attempt_id)
            )
        ).scalar_one()
        sibling = await seed.attempt(node_id, number=2)
        for index in range(64):
            await save_usage(connection, seed, attempt_id, f"model-{index:02}")
            await seed.usage(sibling, f"other-{index:02}", Decimal("9"))
        await connection.execute(
            update(usage_record)
            .where(usage_record.c.attempt_id == attempt_id)
            .values(usd_estimate=None)
        )
        foreign_namespace = await seed.namespace("foreign")

    async with database.read_transaction() as connection:
        inventory = await read_attempt_usage(connection, namespace_id, attempt_id)
        foreign = await read_attempt_usage(connection, foreign_namespace, attempt_id)

    assert len(inventory) == 64
    assert tuple(item.row.model for item in inventory) == tuple(
        f"model-{index:02}" for index in range(64)
    )
    assert all(item.usd_estimate is None for item in inventory)
    assert all(item.same_basis_usd_floor == "2" for item in inventory)
    assert inventory[0].row.model_dump(mode="json")["source"] == {
        "nul\0": [1, 1.0, -0.0]
    }
    assert foreign == ()


@pytest.mark.asyncio
async def test_usage_inventory_refuses_overflow_without_truncating(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id, attempt_id = await usage_scope(seed)
        for index in range(65):
            await save_usage(connection, seed, attempt_id, f"model-{index:02}")

    async with database.read_transaction() as connection:
        with pytest.raises(CostReadRefused):
            await read_attempt_usage(connection, namespace_id, attempt_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"same_basis_usd_floor": None},
        {"same_basis_usd_floor": Decimal("1")},
        {"same_basis_usd_floor": Decimal("3")},
        {"usd_estimate": None, "billing_class": "subscription"},
        {"dispatcher_id": None},
        {"dispatcher_id": "invalid"},
        {"source": {"value": "x" * 8192}},
        {"source": {"value": "x" * 60_000}},
        {"source": ["invalid object"]},
        {"known_token_fields": ["invalid"]},
        {
            "normalization_version": None,
            "reported_usd": None,
            "known_token_fields": null(),
            "cache_creation_by_ttl": null(),
            "observation_id": None,
            "observation_sequence": None,
            "observed_at": None,
            "dispatcher_id": None,
        },
    ],
)
async def test_usage_inventory_refuses_invalid_saved_accounting(
    database: Database, changes: dict[str, object]
) -> None:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id, attempt_id = await usage_scope(seed)
        await save_usage(connection, seed, attempt_id, "model", **changes)

    async with database.read_transaction() as connection:
        with pytest.raises(CostReadRefused):
            await read_attempt_usage(connection, namespace_id, attempt_id)


@pytest.mark.asyncio
async def test_usage_inventory_accepts_unknown_historical_floor(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id, attempt_id = await usage_scope(seed)
        await save_usage(
            connection,
            seed,
            attempt_id,
            "model",
            usd_estimate=None,
            same_basis_usd_floor=None,
            billing_class="subscription",
        )

    async with database.read_transaction() as connection:
        inventory = await read_attempt_usage(connection, namespace_id, attempt_id)

    assert len(inventory) == 1
    assert inventory[0].usd_estimate is None
    assert inventory[0].same_basis_usd_floor is None


@pytest.mark.asyncio
async def test_usage_inventory_uses_attempt_index_with_irrelevant_history(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id, attempt_id = await usage_scope(seed)
        node_id = (
            await connection.execute(
                select(attempt.c.node_instance_id).where(attempt.c.id == attempt_id)
            )
        ).scalar_one()
        await save_usage(connection, seed, attempt_id, "selected")
        unrelated: list[dict[str, object]] = []
        for number in range(2, 34):
            sibling = await seed.attempt(node_id, number=number)
            for model in range(64):
                values = usage_payload(model=f"model-{model:02}")
                values.update(
                    id=seed.new_id(),
                    attempt_id=sibling,
                    normalization_version=1,
                    dispatcher_id=seed.new_id(),
                    usd_estimate=None,
                    same_basis_usd_floor=None,
                    **seed.stamps(),
                )
                unrelated.append(values)
        await connection.execute(insert(usage_record), unrelated)
        attempt_template: dict[str, object] = dict(
            (
                await connection.execute(
                    select(attempt).where(attempt.c.id == attempt_id)
                )
            )
            .mappings()
            .one()
        )
        unrelated_attempts: list[dict[str, object]] = [
            {**attempt_template, "id": seed.new_id(), "number": number}
            for number in range(34, 2082)
        ]
        await connection.execute(insert(attempt), unrelated_attempts)
        await connection.exec_driver_sql("ANALYZE attempt")
        await connection.exec_driver_sql("ANALYZE usage_record")
    statements: list[tuple[str, tuple[object, ...]]] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        *arguments: object,
    ) -> None:
        if "FROM usage_record" in statement:
            statements.append((statement, parameters))

    async with database.read_transaction() as connection:
        event.listen(connection.sync_connection, "before_cursor_execute", capture)
        try:
            inventory = await read_attempt_usage(connection, namespace_id, attempt_id)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", capture)
        statement, parameters = statements[0]
        if database.dialect is Dialect.POSTGRESQL:
            await connection.exec_driver_sql(
                "SET LOCAL plan_cache_mode = force_generic_plan"
            )
            await connection.exec_driver_sql(f"PREPARE kora_usage_probe AS {statement}")
            arguments = ", ".join(
                str(
                    literal(value).compile(
                        dialect=connection.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                )
                for value in parameters
            )
            plan = "\n".join(
                (
                    await connection.exec_driver_sql(
                        f"EXPLAIN EXECUTE kora_usage_probe({arguments})"
                    )
                ).scalars()
            )
            await connection.exec_driver_sql("DEALLOCATE kora_usage_probe")
        else:
            plan = "\n".join(
                str(row)
                for row in await connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                )
            )

    assert len(statements) == 1
    assert [item.row.model for item in inventory] == ["selected"]
    if database.dialect is Dialect.POSTGRESQL:
        assert "uq_usage_record_attempt_id_model" in plan, plan
        assert "Index Cond: ((attempt_id)::text =" in plan, plan
        assert "Seq Scan on usage_record" not in plan, plan
        for relation in ("attempt", "node_instance", "run"):
            assert f"pk_{relation} on {relation}" in plan, plan
    else:
        assert "SEARCH usage_record USING INDEX" in plan, plan
        assert "(attempt_id=?)" in plan, plan
        assert "SCAN usage_record" not in plan, plan
