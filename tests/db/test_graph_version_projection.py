import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.graph_version_projection import (
    SQLiteEncoding,
    graph_version_admitted_projection_statement,
)
from kora.db.graph_version_reads import MAX_SQL_LENGTH
from kora.db.migrations import upgrade_database
from kora.db.tables import GRAPH_VERSION_SCOPE_INDEX, graph_version
from kora.db.types import JsonObject
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.support.databases import DatabaseKind, encoded_sqlite_settings

IR_DIGEST = "1" * 64
CONTRACT_DIGEST = "2" * 64
DEPLOYMENT_DIGEST = "3" * 64
RESOURCES: JsonObject = {"uri": "é"}
SQLITE_FIELD_ORDINALS = (3, 4, 6, 7, 8, 10)
SQLITE_COLUMN_TYPE_ONLY = 0x80
SQLITE_COLUMN_LENGTH_ONLY = 0xC0


@dataclass(frozen=True)
class Scope:
    namespace: str
    graph: str
    version: str


def expected_values(
    ir: str, sqlite_encoding: SQLiteEncoding | None
) -> tuple[bytes, ...]:
    encoding = sqlite_encoding or "utf-8"
    resources = '{"uri":"é"}' if sqlite_encoding is not None else '{"uri": "é"}'
    return tuple(
        value.encode(encoding)
        for value in (
            json.dumps(ir),
            IR_DIGEST,
            json.dumps("xyz"),
            CONTRACT_DIGEST,
            DEPLOYMENT_DIGEST,
            resources,
        )
    )


async def store(
    seeder: Seeder, graph: str, number: int, ir: str, *, marker: int | None = 1
) -> str:
    return await seeder.graph_version(
        graph,
        number,
        ir,
        execution_contract="xyz",
        value_encoding_version=marker,
        ir_digest=IR_DIGEST,
        source_digest=IR_DIGEST,
        execution_contract_digest=CONTRACT_DIGEST,
        deployment_digest=DEPLOYMENT_DIGEST,
        resources=RESOURCES,
    )


async def project(
    connection: AsyncConnection,
    dialect: Dialect,
    scope: Scope,
    encoding: SQLiteEncoding | None,
    resource_bytes: int,
    *,
    pair_limit: int = 3,
) -> tuple[object, ...] | None:
    statement = graph_version_admitted_projection_statement(
        dialect,
        scope.namespace,
        scope.graph,
        scope.version,
        sqlite_encoding=encoding,
        max_encoded_bytes=pair_limit,
        resource_transport_bytes=resource_bytes,
    )
    row = (await connection.execute(statement)).one_or_none()
    return None if row is None else tuple(row)


async def check_boundaries(
    database: Database, dialect: Dialect, encoding: SQLiteEncoding | None
) -> None:
    expected = expected_values("abc", encoding)
    resource_bytes = len(expected[-1])
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace()
        other_namespace = await seeder.namespace("other")
        graph = await seeder.graph(namespace)
        other_graph = await seeder.graph(namespace, "other")
        version = await store(seeder, graph, 1, "abc")
        too_wide = await store(seeder, graph, 2, "abcd")
        legacy = await store(seeder, graph, 3, "abc", marker=None)
        bad_native_ir = await store(seeder, graph, 4, "abc")
        bad_native_contract = await store(seeder, graph, 5, "abc")
        bad_native_resources = await store(seeder, graph, 6, "abc")
        bad_digests = [
            await store(seeder, graph, number, "abc") for number in (7, 8, 9)
        ]
        missing = seeder.new_id()
        for field, malformed in zip(
            ("ir_digest", "execution_contract_digest", "deployment_digest"),
            bad_digests,
            strict=True,
        ):
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.id == malformed)
                .values({field: "0" * 63})
            )
        if dialect is Dialect.SQLITE:
            await writer.exec_driver_sql(
                "UPDATE graph_version SET ir = CAST(ir AS BLOB) WHERE id = ?",
                (bad_native_ir,),
            )
            await writer.exec_driver_sql(
                "UPDATE graph_version SET execution_contract = "
                "CAST(execution_contract AS BLOB) WHERE id = ?",
                (bad_native_contract,),
            )
            await writer.exec_driver_sql(
                "UPDATE graph_version SET resources = CAST(resources AS BLOB) "
                "WHERE id = ?",
                (bad_native_resources,),
            )
        else:
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.id == bad_native_ir)
                .values(ir=True)
            )
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.id == bad_native_contract)
                .values(execution_contract=True)
            )
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.id == bad_native_resources)
                .values(resources=["123456789"])
            )
    scope = Scope(namespace, graph, version)

    async with database.read_snapshot() as reader:
        accepted = await project(reader, dialect, scope, encoding, resource_bytes)
        accepted_at_maximum = await project(
            reader, dialect, scope, encoding, resource_bytes, pair_limit=MAX_SQL_LENGTH
        )
        refused = [
            await project(reader, dialect, candidate, encoding, resource_bytes)
            for candidate in (
                replace(scope, namespace=other_namespace),
                replace(scope, graph=other_graph),
                *(
                    replace(scope, version=identifier)
                    for identifier in (
                        too_wide,
                        legacy,
                        bad_native_ir,
                        bad_native_contract,
                        bad_native_resources,
                        *bad_digests,
                        missing,
                    )
                ),
            )
        ]
        too_short = await project(reader, dialect, scope, encoding, resource_bytes - 1)
        too_long = await project(reader, dialect, scope, encoding, resource_bytes + 1)
        huge_resource_width = await project(
            reader, dialect, scope, encoding, MAX_SQL_LENGTH
        )

    assert accepted == accepted_at_maximum == expected
    assert all(
        value is None for value in (*refused, too_short, too_long, huge_resource_width)
    )
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_combined_graph_projection_transfers_only_the_exact_admitted_width(
    database: Database, database_kind: DatabaseKind
) -> None:
    dialect = Dialect(database_kind.value)
    await check_boundaries(
        database, dialect, "utf-8" if dialect is Dialect.SQLITE else None
    )


@pytest.mark.parametrize("encoding", ["utf-16le", "utf-16be"])
@pytest.mark.asyncio
async def test_combined_graph_projection_uses_sqlite_native_byte_widths(
    tmp_path: Path, encoding: SQLiteEncoding
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "wide.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    ledger = ConnectionLedger(database)

    try:
        await check_boundaries(database, Dialect.SQLITE, encoding)
    finally:
        await database.close()

    assert ledger.opened == ledger.closed > 0


def assert_sqlite_projection_program(
    instructions: list[tuple[object, ...]], root_page: object
) -> None:
    (table_cursor,) = (
        row[2] for row in instructions if row[1] == "OpenRead" and row[3] == root_page
    )
    native_columns = [
        row for row in instructions if row[1] == "Column" and row[2] == table_cursor
    ]
    assert [(row[3], row[6]) for row in native_columns] == [
        (16, SQLITE_COLUMN_TYPE_ONLY),
        (16, 0),
        *((ordinal, SQLITE_COLUMN_TYPE_ONLY) for ordinal in SQLITE_FIELD_ORDINALS),
        *((ordinal, SQLITE_COLUMN_LENGTH_ONLY) for ordinal in (3, 6, 4, 7, 8, 10)),
        *((ordinal, 0) for ordinal in SQLITE_FIELD_ORDINALS),
    ]
    for row in native_columns:
        address = row[0]
        assert type(address) is int
        following = instructions[address + 1]
        if row[6] == SQLITE_COLUMN_TYPE_ONLY:
            assert following[1] == "Function"
            assert following[5] == "typeof(1)"
        elif row[6] == SQLITE_COLUMN_LENGTH_ONLY:
            assert following[1] == "Function"
            assert following[5] == "octet_length(1)"
        elif row[3] != 16:
            assert following[1] == "Cast"
    first_projection = native_columns[-len(SQLITE_FIELD_ORDINALS)][0]
    (result,) = (row for row in instructions if row[1] == "ResultRow")
    assert type(first_projection) is int
    assert type(result[0]) is int
    guard = instructions[first_projection - 1]
    assert guard[1] == "IfNot"
    assert type(guard[3]) is int
    assert guard[3] > result[0]
    marker_type = native_columns[0][0]
    assert type(marker_type) is int
    assert instructions[marker_type + 4][1:4] == ("Goto", 0, guard[0])


async def check_sqlite_projection_plan(database: Database) -> None:
    expected = expected_values("abc", "utf-8")
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = await store(seeder, graph, 1, "abc")
        bad_marker_type = await store(seeder, graph, 2, "abc")
        bad_marker_value = await store(seeder, graph, 3, "abc")
        await writer.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        try:
            await writer.exec_driver_sql(
                "UPDATE main.graph_version SET value_encoding_version = ? WHERE id = ?",
                (b"1", bad_marker_type),
            )
            await writer.exec_driver_sql(
                "UPDATE main.graph_version SET value_encoding_version = ? WHERE id = ?",
                (2, bad_marker_value),
            )
        finally:
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
        root_page: object = (
            await writer.exec_driver_sql(
                "SELECT rootpage FROM main.sqlite_schema WHERE name = 'graph_version'"
            )
        ).scalar_one()
        await writer.exec_driver_sql("CREATE TEMP TABLE graph (id TEXT)")
        await writer.exec_driver_sql("CREATE TEMP TABLE graph_version (id TEXT)")

        try:
            with executed_queries(writer) as calls:
                accepted = await project(
                    writer,
                    Dialect.SQLITE,
                    Scope(namespace, graph, version),
                    "utf-8",
                    len(expected[-1]),
                )
            query, arguments = calls[0]
            plan: list[tuple[object, ...]] = [
                tuple(row)
                for row in (
                    await writer.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + query, arguments
                    )
                )
            ]
            instructions = await sqlite_instructions(writer, query, arguments)
            refused = [
                await project(
                    writer,
                    Dialect.SQLITE,
                    Scope(namespace, graph, identifier),
                    "utf-8",
                    len(expected[-1]),
                )
                for identifier in (bad_marker_type, bad_marker_value)
            ]
        finally:
            await writer.exec_driver_sql("DROP TABLE temp.graph_version")
            await writer.exec_driver_sql("DROP TABLE temp.graph")

    assert accepted == expected
    assert refused == [None, None]
    assert [row[3] for row in plan] == [
        "SEARCH g USING INDEX sqlite_autoindex_graph_1 (id=?)",
        f"SEARCH v USING INDEX {GRAPH_VERSION_SCOPE_INDEX} (graph_id=? AND id=?)",
    ]
    assert_sqlite_projection_program(instructions, root_page)
    assert ledger.outstanding == database.units_in_flight() == 0


class PostgresPlan(BaseModel):
    node_type: str = Field(alias="Node Type")
    rows: int = Field(alias="Actual Rows")
    loops: int = Field(alias="Actual Loops")
    output: list[str] = Field(default_factory=list, alias="Output")
    filter: str = Field(default="", alias="Filter")
    subplan_name: str = Field(default="", alias="Subplan Name")
    children: tuple["PostgresPlan", ...] = Field(default=(), alias="Plans")


class PostgresPlanResult(BaseModel):
    plan: PostgresPlan = Field(alias="Plan")


def parse_postgres_plan(raw: object) -> PostgresPlan:
    parser = TypeAdapter(list[PostgresPlanResult])
    results = (
        parser.validate_json(raw) if type(raw) is str else parser.validate_python(raw)
    )
    (result,) = results
    return result.plan


POSTGRESQL_TOAST_SIZE = (
    "SELECT pg_relation_size(reltoastrelid) FROM pg_class "
    "WHERE oid = 'graph_version'::regclass"
)
POSTGRESQL_STORED_IR = (
    "SELECT pg_column_size(ir), pg_column_compression(ir) "
    "FROM graph_version WHERE id = $1"
)


async def check_postgresql_projection_plan(database: Database) -> None:
    large_ir = "x" * 65_536
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        initial_size: object = (
            await writer.exec_driver_sql(POSTGRESQL_TOAST_SIZE)
        ).scalar_one()
        await writer.exec_driver_sql(
            "ALTER TABLE graph_version ALTER COLUMN ir SET COMPRESSION pglz"
        )
        await writer.exec_driver_sql(
            "ALTER TABLE graph_version ALTER COLUMN ir SET STORAGE PLAIN"
        )
        inline = await store(seeder, graph, 1, "abc")
        await writer.exec_driver_sql(
            "ALTER TABLE graph_version ALTER COLUMN ir SET STORAGE EXTENDED"
        )
        compressed = await store(seeder, graph, 2, large_ir)
        after_compression: object = (
            await writer.exec_driver_sql(POSTGRESQL_TOAST_SIZE)
        ).scalar_one()
        await writer.exec_driver_sql(
            "ALTER TABLE graph_version ALTER COLUMN ir SET STORAGE EXTERNAL"
        )
        external = await store(seeder, graph, 3, large_ir)
        after_external: object = (
            await writer.exec_driver_sql(POSTGRESQL_TOAST_SIZE)
        ).scalar_one()
        physical = [
            tuple(
                (
                    await writer.exec_driver_sql(POSTGRESQL_STORED_IR, (identifier,))
                ).one()
            )
            for identifier in (inline, compressed, external)
        ]
        await writer.exec_driver_sql(
            "CREATE TABLE graph_version_child () INHERITS (graph_version)"
        )
        await writer.exec_driver_sql(
            "INSERT INTO graph_version_child SELECT * FROM graph_version WHERE id = $1",
            (inline,),
        )

    expected = [expected_values(ir, None) for ir in ("abc", large_ir, large_ir)]
    projected: list[tuple[object, ...] | None] = []
    plans: list[PostgresPlan] = []
    async with database.read_snapshot() as reader:
        for identifier, values in zip(
            (inline, compressed, external), expected, strict=True
        ):
            with executed_queries(reader) as calls:
                projected.append(
                    await project(
                        reader,
                        Dialect.POSTGRESQL,
                        Scope(namespace, graph, identifier),
                        None,
                        len(values[-1]),
                        pair_limit=len(large_ir),
                    )
                )
            query, arguments = calls[0]
            raw: object = (
                await reader.exec_driver_sql(
                    "EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + query, arguments
                )
            ).scalar_one()
            plans.append(parse_postgres_plan(raw))

    assert projected == expected
    assert initial_size == after_compression == 0
    assert type(after_external) is int
    assert after_external > 0
    assert physical[0][1] is None
    assert physical[1][1] == "pglz"
    assert physical[2][1] is None
    sizes = [row[0] for row in physical]
    assert all(type(size) is int for size in sizes)
    inline_size, compressed_size, external_size = (int(str(size)) for size in sizes)
    assert inline_size < compressed_size < external_size
    assert external_size >= len(large_ir)
    for plan in plans:
        assert plan.node_type == "CTE Scan"
        assert (plan.rows, plan.loops) == (1, 1)
        (producer,) = plan.children
        assert producer.subplan_name == "CTE rendered"
        assert (producer.rows, producer.loops) == (1, 1)
        assert len(producer.output) == 6
        assert all(field.count("convert_to(") == 1 for field in producer.output)
        assert len(plan.output) == 6
        assert all("convert_to(" not in field for field in plan.output)
        assert plan.filter.count("octet_length(") == 6
        assert "convert_to(" not in plan.filter
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_combined_graph_projection_native_plan_and_representation(
    database: Database, database_kind: DatabaseKind
) -> None:
    if database_kind is DatabaseKind.SQLITE:
        await check_sqlite_projection_plan(database)
    else:
        await check_postgresql_projection_plan(database)
