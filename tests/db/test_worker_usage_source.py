import math
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import (
    Connection,
    MetaData,
    Table,
    Text,
    cast,
    insert,
    null,
    select,
    update,
)

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.cost_reads import CostReadRefused, read_usage_source
from kora.db.migrations import upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import usage_record
from kora.db.types import TIMESTAMP_TEXT_FORMAT, JsonValue
from kora.db.usage_source import source_text
from tests.db.seed import START, Seeder
from tests.ir.test_cost_models import TOKENS


async def stored_source(database: Database, source: JsonValue) -> tuple[str, str]:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        run_id = await seed.run(namespace_id, graph_id, version_id)
        node_id = await seed.node(run_id, "usage")
        attempt_id = await seed.attempt(node_id)
        usage_id = await seed.usage(attempt_id, "model", Decimal("1"))
        await connection.execute(
            update(usage_record)
            .where(usage_record.c.id == usage_id)
            .values(
                source=source,
                normalization_version=1,
                known_token_fields=TOKENS,
                observation_id="observation",
                observation_sequence=0,
                observed_at=START,
                same_basis_usd_floor=Decimal("1"),
            )
        )
    return namespace_id, usage_id


@pytest.mark.asyncio
async def test_usage_source_preserves_nul_and_numeric_identity(
    database: Database,
) -> None:
    source: JsonValue = {"nul\0": ["é\0😀", 1, 1.0, -0.0, 1 << 1000]}
    namespace_id, usage_id = await stored_source(database, source)

    async with database.read_transaction() as connection:
        actual = await read_usage_source(connection, namespace_id, usage_id)
        encoded = (
            await connection.execute(
                select(cast(usage_record.c.source, Text)).where(
                    usage_record.c.id == usage_id
                )
            )
        ).scalar_one()

    assert actual == source
    assert isinstance(actual, dict)
    values = actual["nul\0"]
    assert isinstance(values, list)
    assert type(values[1]) is int
    assert type(values[2]) is float
    assert isinstance(values[3], float)
    assert math.copysign(1, values[3]) == -1
    assert encoded.isascii()
    assert "\\u0000" in encoded
    assert "\0" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("character", ["a", "é", "\0"])
async def test_usage_source_exact_bound_and_overflow(
    database: Database, character: str
) -> None:
    overhead = len(source_text({"value": ""}, ascii_only=False).encode())
    unit = len(source_text(character, ascii_only=False).encode()) - 2
    count, remainder = divmod(8192 - overhead, unit)
    value = character * count + "a" * remainder
    source: JsonValue = {"value": value}
    namespace_id, usage_id = await stored_source(database, source)

    async with database.read_transaction() as connection:
        exact = await read_usage_source(connection, namespace_id, usage_id)
    async with database.transaction() as connection:
        await connection.execute(
            update(usage_record)
            .where(usage_record.c.id == usage_id)
            .values(source={"value": value + "a"})
        )
    async with database.read_transaction() as connection:
        with pytest.raises(CostReadRefused):
            await read_usage_source(connection, namespace_id, usage_id)

    assert exact == source
    assert len(source_text(exact, ascii_only=False).encode()) == 8192


def predecessor(connection: Connection) -> Table:
    command.upgrade(alembic_config(connection), "0023")
    return Table(
        "usage_record", MetaData(), autoload_with=connection, resolve_fks=False
    )


@pytest.mark.asyncio
async def test_usage_migration_preserves_source_and_available_floor(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    originals: dict[str, JsonValue] = {}
    token_fields: list[JsonValue] = list(TOKENS)
    try:
        async with database.migration_connection() as connection:
            previous = await connection.run_sync(predecessor)
        async with database.transaction() as connection:
            seed = Seeder(connection)
            namespace_id = await seed.namespace()
            graph_id = await seed.graph(namespace_id)
            version_id = await seed.graph_version(graph_id)
            run_id = await seed.run(namespace_id, graph_id, version_id)
            node_id = await seed.node(run_id, "usage")
            attempt_id = await seed.attempt(node_id)
            timestamp = (
                START.strftime(TIMESTAMP_TEXT_FORMAT)
                if database.dialect is Dialect.SQLITE
                else START
            )
            for model, normalized, amount in (
                ("legacy", False, Decimal("9")),
                ("known", True, Decimal("2")),
                ("unknown", True, None),
            ):
                usage_id = seed.new_id()
                source: JsonValue = {"model": model, "values": [1, 1.0, "é"]}
                originals[usage_id] = source
                await connection.execute(
                    insert(previous).values(
                        id=usage_id,
                        attempt_id=attempt_id,
                        model=model,
                        input_tokens=1,
                        cached_input_tokens=0,
                        cache_creation_tokens=0,
                        output_tokens=1,
                        reasoning_tokens=0,
                        completeness="partial",
                        basis="price_table",
                        billing_class="api_key",
                        usd_estimate=(
                            str(amount)
                            if amount is not None and database.dialect is Dialect.SQLITE
                            else amount
                        ),
                        delta_of_session_totals=False,
                        source=source_text(source)
                        if database.dialect is Dialect.SQLITE
                        else source,
                        created_at=timestamp,
                        updated_at=timestamp,
                        normalization_version=1 if normalized else None,
                        known_token_fields=(
                            source_text(token_fields)
                            if database.dialect is Dialect.SQLITE
                            else TOKENS
                        )
                        if normalized
                        else null(),
                        observation_id="observation" if normalized else None,
                        observation_sequence=0 if normalized else None,
                        observed_at=timestamp if normalized else None,
                    )
                )
        await database.close()

        report = await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.read_transaction() as connection:
            rows = (
                (
                    await connection.execute(
                        select(
                            usage_record.c.id,
                            usage_record.c.model,
                            usage_record.c.source,
                            usage_record.c.same_basis_usd_floor,
                        )
                    )
                )
                .mappings()
                .all()
            )
            decoded = {
                usage_id: await read_usage_source(connection, namespace_id, usage_id)
                for usage_id in originals
            }
        repeat = await upgrade_database(empty_database_settings)

        assert report.from_revision == "0023"
        assert report.to_revision == "0027"
        assert repeat.steps == 0
        assert decoded == originals
        assert {row["id"]: row["source"] for row in rows} == originals
        assert {row["model"]: row["same_basis_usd_floor"] for row in rows} == {
            "legacy": None,
            "known": Decimal("2"),
            "unknown": None,
        }
    finally:
        await database.close()
