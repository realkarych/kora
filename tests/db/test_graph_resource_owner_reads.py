import pickle
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.graph_resource_owner_reads import (
    EMPTY,
    MAX_OWNER_ROWS,
    AcquiredGraphResourceOwners,
    NativeOwnerRow,
    acquire_graph_resource_owners,
    graph_resource_owner_statement,
)
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import MAX_SQL_LENGTH
from kora.db.migrations import upgrade_database
from kora.db.tables import graph_version
from kora.db.tables import graph_version_resource_owner as owner
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.db.test_graph_version_resource_owner_migration import (
    owner_values,
    seed_versions,
)
from tests.engine.test_graph_command_manifests import ZERO
from tests.support.databases import encoded_sqlite_settings

PRIVATE_NAME = "assets/private-𐐀é.sh"
OWNER_INDEX = "sqlite_autoindex_graph_version_resource_owner_1"
POSTGRESQL_OWNER_KEY = "pk_graph_version_resource_owner"
SQLITE_LENGTH_ONLY = 0xC0


def wire_rows(encoding: str) -> tuple[NativeOwnerRow, ...]:
    def encode(value: str) -> bytes:
        return value.encode(encoding)

    return (
        (
            0,
            0,
            None,
            encode("producer"),
            encode(PRIVATE_NAME),
            encode("Store"),
            encode(ZERO),
            encode("Provider-Original"),
            encode("present"),
        ),
        (
            1,
            42,
            encode("body"),
            encode("producer"),
            encode("z.sh"),
            encode("Store"),
            encode(ZERO),
            encode("Provider-Original"),
            encode("expired"),
        ),
    )


async def store_two(connection: AsyncConnection, version: str) -> None:
    await connection.execute(
        insert(owner),
        [
            {**owner_values(version), "file_name": PRIVATE_NAME},
            {
                **owner_values(version),
                "ordinal": 1,
                "subgraph_name": "body",
                "file_name": "z.sh",
                "total_size": 42,
                "payload_state": "expired",
            },
        ],
    )


async def raw_rows(
    connection: AsyncConnection, dialect: Dialect, version: str, allowance: int
) -> tuple[tuple[object, ...], ...]:
    result = await connection.execute(
        graph_resource_owner_statement(dialect, version, max_text_bytes=allowance)
    )
    return tuple(tuple(row) for row in result.all())


async def read_and_require_native_widths(database: Database, encoding: str) -> None:
    version, empty_version = await seed_versions(database)
    expected = wire_rows(encoding)
    size = sum(len(value) for row in expected for value in row if type(value) is bytes)
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        await store_two(writer, version)
    async with database.read_snapshot() as reader:
        encoding_query = (
            "PRAGMA main.encoding"
            if database.dialect is Dialect.SQLITE
            else "SHOW server_encoding"
        )
        native_encoding: object = (
            await reader.exec_driver_sql(encoding_query)
        ).scalar_one()

        with executed_queries(reader) as statements:
            batch = await acquire_graph_resource_owners(
                reader, version, max_text_bytes=size
            )
        oversized_allowance = await acquire_graph_resource_owners(
            reader, version, max_text_bytes=MAX_SQL_LENGTH + 1
        )
        empty = await acquire_graph_resource_owners(
            reader, empty_version, max_text_bytes=1
        )
        marker = await raw_rows(reader, database.dialect, empty_version, 1)
        narrow = await raw_rows(reader, database.dialect, version, size - 1)
        with pytest.raises(GraphValueRejected) as refused:
            await acquire_graph_resource_owners(
                reader, version, max_text_bytes=size - 1
            )
        assert ledger.outstanding == database.units_in_flight() == 1

    assert len(statements) == 1
    assert batch.version_id == oversized_allowance.version_id == version
    assert batch.rows == oversized_allowance.rows == expected
    assert empty.version_id == empty_version
    assert empty.rows == ()
    assert marker == ((EMPTY, *((None,) * 9)),)
    assert narrow == ((GraphValueFailure.BUDGET.value, *((None,) * 9)),)
    assert refused.value.reason is GraphValueFailure.BUDGET
    assert refused.value.__context__ is None
    assert type(native_encoding) is str
    assert native_encoding.lower() == (
        "utf8" if database.dialect is Dialect.POSTGRESQL else encoding
    )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert PRIVATE_NAME not in repr(batch)
    with pytest.raises(TypeError, match="require explicit access"):
        pickle.dumps(batch)
    with pytest.raises(ValueError, match="not iterable"):
        jsonable_encoder(batch)


@pytest.mark.asyncio
async def test_graph_owner_batch_exact_native_width_and_refusal(
    database: Database,
) -> None:
    await read_and_require_native_widths(database, "utf-8")


@pytest.mark.parametrize("encoding", ["utf-16le", "utf-16be"])
@pytest.mark.asyncio
async def test_graph_owner_batch_preserves_sqlite_native_encoding(
    tmp_path: Path, encoding: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "owners.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)

    try:
        await read_and_require_native_widths(database, encoding)
    finally:
        await database.close()


async def malformed_fixture(
    database: Database,
) -> tuple[str, str, str, str]:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        sparse, overflow, malformed, oversized = [
            await seeder.graph_version(graph, number) for number in (1, 2, 3, 4)
        ]
        await writer.execute(
            insert(owner),
            [
                owner_values(sparse),
                {**owner_values(sparse), "ordinal": 2},
            ],
        )
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        else:
            await writer.exec_driver_sql(
                "ALTER TABLE graph_version_resource_owner DROP CONSTRAINT "
                "ck_graph_version_resource_owner_ordinal_range, DROP CONSTRAINT "
                "ck_graph_version_resource_owner_size_range"
            )
        try:
            await writer.execute(
                insert(owner),
                [
                    {**owner_values(overflow), "ordinal": number}
                    for number in range(MAX_OWNER_ROWS + 1)
                ],
            )
            await store_two(writer, malformed)
            if database.dialect is Dialect.SQLITE:
                await writer.execute(
                    text(
                        "UPDATE main.graph_version_resource_owner "
                        "SET file_name = CAST(file_name AS BLOB) "
                        "WHERE graph_version_id = :version AND ordinal = 1"
                    ),
                    {"version": malformed},
                )
            else:
                await writer.execute(
                    update(owner)
                    .where(owner.c.graph_version_id == malformed, owner.c.ordinal == 1)
                    .values(total_size=262_145)
                )
        finally:
            if database.dialect is Dialect.SQLITE:
                await writer.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
        await store_two(writer, oversized)
        await writer.execute(
            update(owner)
            .where(owner.c.graph_version_id == oversized, owner.c.ordinal == 1)
            .values(file_name="x" * 262_144)
        )
    return sparse, overflow, malformed, oversized


@pytest.mark.asyncio
async def test_graph_owner_batch_refusal_suppresses_all_fields(
    database: Database,
) -> None:
    sparse, overflow, invalid_size, oversized = await malformed_fixture(database)
    ledger = ConnectionLedger(database)
    observations: list[tuple[tuple[object, ...], ...]] = []
    reasons: list[GraphValueFailure] = []

    async with database.read_snapshot() as reader:
        for version in (sparse, overflow, invalid_size, oversized):
            observations.append(await raw_rows(reader, database.dialect, version, 4096))
            with pytest.raises(GraphValueRejected) as refused:
                await acquire_graph_resource_owners(
                    reader, version, max_text_bytes=4096
                )
            reasons.append(refused.value.reason)

    expected = [GraphValueFailure.INVALID] * 3 + [GraphValueFailure.BUDGET]
    assert observations == [((reason.value, *((None,) * 9)),) for reason in expected]
    assert reasons == expected
    assert ledger.outstanding == database.units_in_flight() == 0


async def read_original_despite_decoys(
    connection: AsyncConnection, version: str, decoy_only: str
) -> tuple[AcquiredGraphResourceOwners, AcquiredGraphResourceOwners]:
    admitted = await acquire_graph_resource_owners(
        connection, version, max_text_bytes=4096
    )
    empty = await acquire_graph_resource_owners(
        connection, decoy_only, max_text_bytes=4096
    )
    return admitted, empty


@pytest.mark.asyncio
async def test_graph_owner_batch_excludes_non_original_relations(
    database: Database,
) -> None:
    version, decoy_only = await seed_versions(database)
    ledger = ConnectionLedger(database)
    dialect = database.dialect
    admitted: AcquiredGraphResourceOwners | None = None
    empty: AcquiredGraphResourceOwners | None = None
    decoy: object = None
    async with database.transaction() as writer:
        await store_two(writer, version)
        if dialect is Dialect.SQLITE:
            await writer.exec_driver_sql(
                "CREATE TEMP TABLE graph_version_resource_owner AS "
                "SELECT * FROM main.graph_version_resource_owner WHERE ordinal = 0"
            )
            try:
                await writer.execute(
                    text(
                        "UPDATE temp.graph_version_resource_owner "
                        "SET graph_version_id = :version, file_name = 'decoy'"
                    ),
                    {"version": decoy_only},
                )
                await writer.exec_driver_sql(
                    "INSERT INTO temp.graph_version_resource_owner "
                    "SELECT * FROM main.graph_version_resource_owner WHERE ordinal = 0"
                )

                admitted, empty = await read_original_despite_decoys(
                    writer, version, decoy_only
                )
                decoy = (
                    await writer.execute(
                        text(
                            "SELECT file_name FROM graph_version_resource_owner "
                            "WHERE graph_version_id = :version"
                        ),
                        {"version": decoy_only},
                    )
                ).scalar_one()
            finally:
                await writer.exec_driver_sql(
                    "DROP TABLE temp.graph_version_resource_owner"
                )
        else:
            await writer.exec_driver_sql(
                "CREATE TABLE graph_owner_child () "
                "INHERITS (graph_version_resource_owner)"
            )
            await writer.exec_driver_sql(
                "INSERT INTO graph_owner_child "
                "SELECT * FROM ONLY graph_version_resource_owner "
                "WHERE ordinal = 0"
            )
            await writer.execute(
                text(
                    "UPDATE ONLY graph_owner_child "
                    "SET graph_version_id = :version, file_name = 'decoy'"
                ),
                {"version": decoy_only},
            )
            await writer.exec_driver_sql(
                "INSERT INTO graph_owner_child "
                "SELECT * FROM ONLY graph_version_resource_owner WHERE ordinal = 0"
            )
    if dialect is Dialect.POSTGRESQL:
        async with database.read_snapshot() as reader:
            admitted, empty = await read_original_despite_decoys(
                reader, version, decoy_only
            )
            decoy = (
                await reader.execute(
                    text(
                        "SELECT file_name FROM graph_version_resource_owner "
                        "WHERE graph_version_id = :id"
                    ),
                    {"id": decoy_only},
                )
            ).scalar_one()

    assert decoy == "decoy"
    assert admitted is not None
    assert empty is not None
    assert admitted.rows == wire_rows("utf-8")
    assert empty.rows == ()
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_owner_batch_uses_the_already_established_snapshot(
    database: Database,
) -> None:
    version, sibling = await seed_versions(database)
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        await store_two(writer, version)
        await writer.execute(
            insert(owner).values(
                **{**owner_values(sibling), "payload_state": "expiring"}
            )
        )

    async with database.read_snapshot() as reader:
        established: object = (
            await reader.execute(
                select(graph_version.c.id).where(graph_version.c.id == version)
            )
        ).scalar_one()
        async with database.transaction() as writer:
            await writer.execute(
                update(owner)
                .where(owner.c.graph_version_id == version, owner.c.ordinal == 0)
                .values(payload_state="missing")
            )
        old = await acquire_graph_resource_owners(reader, version, max_text_bytes=4096)
    async with database.read_snapshot() as reader:
        fresh = await acquire_graph_resource_owners(
            reader, version, max_text_bytes=4096
        )
        other = await acquire_graph_resource_owners(
            reader, sibling, max_text_bytes=4096
        )

    assert established == version
    assert old.rows == wire_rows("utf-8")
    assert fresh.rows == ((*old.rows[0][:-1], b"missing"), old.rows[1])
    assert len(other.rows) == 1
    assert other.rows[0][4] == "assets/é.sh".encode()
    assert other.rows[0][-1] == b"expiring"
    assert ledger.outstanding == database.units_in_flight() == 0


class NativePlan(BaseModel):
    node_type: str = Field(alias="Node Type")
    rows: int = Field(alias="Actual Rows")
    loops: int = Field(alias="Actual Loops")
    subplan_name: str = Field(default="", alias="Subplan Name")
    index_name: str = Field(default="", alias="Index Name")
    index_condition: str = Field(default="", alias="Index Cond")
    children: tuple["NativePlan", ...] = Field(default=(), alias="Plans")


class NativePlanResult(BaseModel):
    plan: NativePlan = Field(alias="Plan")


def plan_nodes(plan: NativePlan) -> tuple[NativePlan, ...]:
    return (
        plan,
        *(descendant for child in plan.children for descendant in plan_nodes(child)),
    )


async def postgres_owner_plan(
    connection: AsyncConnection, query: str, arguments: tuple[object, ...]
) -> NativePlan:
    raw: object = (
        await connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + query, arguments
        )
    ).scalar_one()
    adapter = TypeAdapter(list[NativePlanResult])
    (result,) = (
        adapter.validate_json(raw) if type(raw) is str else adapter.validate_python(raw)
    )
    return result.plan


async def sqlite_prefix_program(
    connection: AsyncConnection, query: str, arguments: tuple[object, ...]
) -> tuple[list[str], list[tuple[object, ...]]]:
    plan = await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + query, arguments)
    details = [str(tuple(row)[3]) for row in plan]
    return details, await sqlite_instructions(connection, query, arguments)


def assert_sqlite_width_prefix(
    details: list[str], instructions: list[tuple[object, ...]]
) -> None:
    start = details.index("MATERIALIZE owner_widths")
    assert (
        details[start + 1]
        == f"SEARCH owner USING INDEX {OWNER_INDEX} (graph_version_id=?)"
    )
    assert "ORDER BY" not in details[start + 2] or not details[start + 2].startswith(
        "USE TEMP"
    )
    widths = [
        instructions[index - 1]
        for index, step in enumerate(instructions)
        if step[1] == "Function" and step[5] == "octet_length(1)"
    ]
    assert [step[1] for step in widths] == ["Column"] * 7
    assert [step[3] for step in widths] == [2, 3, 4, 5, 6, 7, 9]
    assert [step[6] for step in widths] == [SQLITE_LENGTH_ONLY] * 7
    assert sum(step[1] == "Cast" for step in instructions) == 7
    seek_position = max(
        index for index, step in enumerate(instructions) if step[1] == "SeekGE"
    )
    seek = instructions[seek_position]
    (null_key,) = (
        step
        for step in instructions[seek_position - 4 : seek_position]
        if step[1] == "IsNull" and step[2] == seek[4]
    )
    assert type(null_key[3]) is int
    assert null_key[3] > seek_position


def assert_postgres_owner_plan(
    plan: NativePlan, identifier: str, prefix_rows: int, rendered_rows: int
) -> None:
    nodes = plan_nodes(plan)
    (prefix,) = (node for node in nodes if node.subplan_name == "CTE owner_widths")
    assert prefix.node_type == "Limit"
    assert (prefix.rows, prefix.loops) == (prefix_rows, 1)
    if prefix_rows > MAX_OWNER_ROWS:
        (seek,) = (
            node
            for node in plan_nodes(prefix)
            if node.index_name == POSTGRESQL_OWNER_KEY
        )
        assert seek.node_type in ("Index Scan", "Bitmap Index Scan")
    else:
        (seek,) = prefix.children
        assert seek.node_type == "Index Scan"
    assert seek.index_name == POSTGRESQL_OWNER_KEY
    assert "graph_version_id" in seek.index_condition
    assert f"'{identifier}'" in seek.index_condition
    assert (seek.rows, seek.loops) == (prefix_rows, 1)
    (transfer,) = (
        node
        for node in nodes
        if node.index_name == POSTGRESQL_OWNER_KEY and "CASE" in node.index_condition
    )
    assert "owner_gate.status" in transfer.index_condition
    assert (transfer.rows, transfer.loops) == (rendered_rows, 1)


@pytest.mark.asyncio
async def test_owner_batch_plan_bounds_the_exact_ordered_prefix(
    database: Database,
) -> None:
    _, overflow, _, _ = await malformed_fixture(database)
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace("plan")
        graph = await seeder.graph(namespace)
        version = await seeder.graph_version(graph)
        empty = await seeder.graph_version(graph, 2)
        await store_two(writer, version)
        unrelated = [
            await seeder.graph_version(graph, number) for number in range(3, 35)
        ]
        await writer.execute(
            insert(owner),
            [
                {**owner_values(identifier), "ordinal": ordinal}
                for identifier in unrelated
                for ordinal in range(MAX_OWNER_ROWS)
            ],
        )
        await writer.exec_driver_sql("ANALYZE graph_version_resource_owner")
    sqlite: list[tuple[list[str], list[tuple[object, ...]]]] = []
    postgres: list[NativePlan] = []

    async with database.read_snapshot() as reader:
        for identifier, allowance in (
            (version, 4096),
            (empty, 4096),
            (version, 1),
            (overflow, 32_768),
        ):
            with executed_queries(reader) as queries:
                await raw_rows(reader, database.dialect, identifier, allowance)
            [(query, arguments)] = queries
            if database.dialect is Dialect.SQLITE:
                sqlite.append(await sqlite_prefix_program(reader, query, arguments))
            else:
                postgres.append(await postgres_owner_plan(reader, query, arguments))
        largest = await acquire_graph_resource_owners(
            reader, unrelated[0], max_text_bytes=32_768
        )

    for details, instructions in sqlite:
        assert_sqlite_width_prefix(details, instructions)
    for (identifier, prefix_rows, rendered_rows), plan in zip(
        ((version, 2, 2), (empty, 0, 0), (version, 2, 0), (overflow, 65, 0))
        if postgres
        else (),
        postgres,
        strict=True,
    ):
        assert_postgres_owner_plan(plan, identifier, prefix_rows, rendered_rows)
    assert [row[0] for row in largest.rows] == list(range(MAX_OWNER_ROWS))
    assert ledger.outstanding == database.units_in_flight() == 0
