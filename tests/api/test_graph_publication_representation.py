import json
from pathlib import Path

import pytest
from sqlalchemy import (
    BigInteger,
    LargeBinary,
    Select,
    Text,
    bindparam,
    func,
    insert,
    select,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ColumnElement

from kora.api.graph_deployment_attachment import attach_graph_deployment_references
from kora.api.graph_publication_values import (
    DEPLOYED_BY_PARAMETER,
    RESOLVED_DIGESTS_PARAMETER,
    PreparedGraphPublicationBinds,
    graph_publication_bind_expressions,
    graph_publication_measurement_statement,
    prepare_graph_publication_binds,
    sqlite_graph_publication_metadata_bind_expressions,
    sqlite_graph_publication_record_measurement_statement,
)
from kora.db.connection import Database, Dialect
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import ObjectReader, TextBudget, render_object
from kora.db.migrations import upgrade_database
from kora.db.tables import graph_version
from kora.db.types import JsonObject, canonical_json
from tests.api.test_graph_deployment_attachment import (
    ATTACH_LIMITS,
    attachment_inputs,
    prepare_inputs,
)
from tests.api.test_graph_execution_preparation import READ_LIMITS
from tests.api.test_graph_publication_values import (
    PRIVATE_WIRE,
    checked_attachment,
    publication_limits,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.test_graph_resource_references import SCOPE
from tests.support.databases import encoded_sqlite_settings

LARGE_TEXT = 32_768
SQLITE_ENCODINGS = ("UTF-16le", "UTF-16be")
SIZE_COLUMNS = (
    "ir_native_bytes",
    "execution_native_bytes",
    "resources_native_bytes",
    "resource_transport_bytes",
)


@pytest.fixture(scope="module")
def measurements_inputs() -> tuple[PreparedGraphPublicationBinds, ...]:
    source, files, owners = attachment_inputs(SCOPE)
    ir = ObjectReader(source).object()
    ir["x-width"] = PRIVATE_WIRE + "x" * LARGE_TEXT
    wide = render_object(ir, TextBudget(READ_LIMITS.identity.max_encoded_bytes))
    attachment = attach_graph_deployment_references(
        prepare_inputs(wide, files), SCOPE, owners, limits=ATTACH_LIMITS
    )
    return tuple(
        prepare_graph_publication_binds(candidate, limits=publication_limits(candidate))
        for candidate in (
            checked_attachment(with_files=True),
            checked_attachment(with_files=False),
            attachment,
        )
    )


def postgres_fresh_size(field: ColumnElement[object]) -> ColumnElement[int]:
    return func.pg_column_size(
        sql_cast(sql_cast(field, Text()), JSONB()), type_=BigInteger()
    )


def stored_measurement_statement(dialect: Dialect) -> Select[int, int, int, bytes]:
    fields = graph_version.c
    physical = graph_resource_binary_projection(dialect, fields.resources)
    if dialect is Dialect.POSTGRESQL:
        return select(
            postgres_fresh_size(fields.ir),
            postgres_fresh_size(fields.execution_contract),
            postgres_fresh_size(fields.resources),
            physical,
        )
    return select(
        func.length(sql_cast(fields.ir, LargeBinary()), type_=BigInteger()),
        func.length(
            sql_cast(fields.execution_contract, LargeBinary()), type_=BigInteger()
        ),
        func.length(physical, type_=BigInteger()),
        physical,
    )


async def postgres_storage_shapes(
    writer: AsyncConnection,
    small: PreparedGraphPublicationBinds,
    wide: PreparedGraphPublicationBinds,
    fresh_small: int,
    fresh_wide: int,
) -> None:
    await writer.exec_driver_sql(
        "CREATE TEMPORARY TABLE publication_representation_storage "
        "(kind TEXT PRIMARY KEY, value JSONB NOT NULL) ON COMMIT DROP"
    )
    await writer.exec_driver_sql(
        "ALTER TABLE publication_representation_storage "
        "ALTER COLUMN value SET COMPRESSION pglz"
    )
    toast = (
        "SELECT pg_relation_size(reltoastrelid) FROM pg_class "
        "WHERE oid = 'pg_temp.publication_representation_storage'::regclass"
    )
    insert = (
        "INSERT INTO publication_representation_storage (kind, value) "
        "VALUES ($1, CAST(CAST($2 AS TEXT) AS JSONB))"
    )
    stored = (
        "SELECT pg_column_size(value), pg_column_compression(value) "
        "FROM publication_representation_storage WHERE kind = $1"
    )

    await writer.exec_driver_sql(
        "ALTER TABLE publication_representation_storage "
        "ALTER COLUMN value SET STORAGE PLAIN"
    )
    await writer.exec_driver_sql(insert, ("inline", small.ir_json))
    inline: tuple[object, ...] = tuple(
        (await writer.exec_driver_sql(stored, ("inline",))).one()
    )
    toast_inline = (await writer.exec_driver_sql(toast)).scalar_one()
    await writer.exec_driver_sql(
        "ALTER TABLE publication_representation_storage "
        "ALTER COLUMN value SET STORAGE EXTENDED"
    )
    await writer.exec_driver_sql(insert, ("compressed", wide.ir_json))
    compressed: tuple[object, ...] = tuple(
        (await writer.exec_driver_sql(stored, ("compressed",))).one()
    )
    toast_compressed = (await writer.exec_driver_sql(toast)).scalar_one()
    await writer.exec_driver_sql(
        "ALTER TABLE publication_representation_storage "
        "ALTER COLUMN value SET STORAGE EXTERNAL"
    )
    await writer.exec_driver_sql(insert, ("external", wide.ir_json))
    external: tuple[object, ...] = tuple(
        (await writer.exec_driver_sql(stored, ("external",))).one()
    )
    toast_external = (await writer.exec_driver_sql(toast)).scalar_one()

    assert type(inline[0]) is int
    assert fresh_small - 3 <= inline[0] <= fresh_small
    assert inline[1] is None
    assert type(compressed[0]) is int
    assert 0 < compressed[0] < fresh_wide
    assert compressed[1] == "pglz"
    assert type(external[0]) is int
    assert fresh_wide - 4 <= external[0] <= fresh_wide
    assert external[1] is None
    assert toast_inline == toast_compressed == 0
    assert type(toast_external) is int
    assert toast_external > 0


async def verify_measurement(
    database: Database, inputs: tuple[PreparedGraphPublicationBinds, ...], encoding: str
) -> ConnectionLedger:
    ledger = ConnectionLedger(database)
    dialect = database.dialect
    statement = graph_publication_measurement_statement(dialect)
    fields = graph_version.c
    insert = (
        graph_version.update()
        .where(
            fields.id
            == bindparam("publication_version_id", type_=Text(), required=True)
        )
        .values(graph_publication_bind_expressions(dialect))
    )
    stored = stored_measurement_statement(dialect)

    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_ids = [
            await seeder.graph_version(graph_id, number)
            for number in range(1, len(inputs) + 1)
        ]
        template = statement.compile(dialect=writer.dialect)
        with executed_queries(writer) as executed:
            results = [
                (await writer.execute(statement, item.parameters())).one()
                for item in inputs
            ]
        for version_id, item in zip(version_ids, inputs, strict=True):
            await writer.execute(
                insert, item.parameters() | {"publication_version_id": version_id}
            )
        if dialect is Dialect.POSTGRESQL:
            await postgres_storage_shapes(
                writer, inputs[0], inputs[2], results[0][0], results[2][0]
            )
    async with database.read_snapshot() as reader:
        rows = (
            await reader.execute(
                stored.where(fields.id.in_(version_ids)).order_by(fields.number)
            )
        ).all()
        observed_encoding = (
            await reader.exec_driver_sql(
                "SHOW server_encoding"
                if dialect is Dialect.POSTGRESQL
                else "PRAGMA encoding"
            )
        ).scalar_one()

    assert type(observed_encoding) is str
    assert observed_encoding.lower().replace("-", "") == encoding.replace("-", "")
    assert tuple(statement.selected_columns.keys()) == SIZE_COLUMNS
    if dialect is Dialect.POSTGRESQL:
        assert not statement.get_final_froms()
    assert template.params == dict.fromkeys(inputs[0].parameters())
    assert len(executed) == len(inputs) == len(results) == len(rows)
    assert len({sql for sql, _ in executed}) == 1
    assert results[0] != results[1]
    assert results[2][0] - results[0][0] > LARGE_TEXT // 2
    for item, measured, row, (sql, params) in zip(
        inputs, results, rows, executed, strict=True
    ):
        assert PRIVATE_WIRE not in sql
        assert len(params) == 3
        assert all(
            any(arg is value for arg in params) for value in item.parameters().values()
        )
        assert all(type(size) is int and size > 0 for size in measured)
        assert tuple(measured[:3]) == tuple(row[:3])
        resource = row[3]
        assert type(resource) is bytes
        assert measured[3] == len(resource)
        if dialect is Dialect.SQLITE:
            assert tuple(measured) == (
                len(item.ir_json.encode(encoding)),
                len(item.execution_json.encode(encoding)),
                len(item.resources_json.encode(encoding)),
                len(resource),
            )
            assert resource == item.resources_json.encode(encoding)
        else:
            native = resource.decode(encoding)
            decoded: object = json.loads(native)
            assert decoded == ObjectReader(item.resources_json).object()
            if "resource_set_digest" in item.resources_json:
                assert item.resources_json.index(
                    '"resource_set_digest"'
                ) < item.resources_json.index('"resources"')
                assert native.index('"resources"') < native.index(
                    '"resource_set_digest"'
                )
                assert "é.sh" in native
    assert ledger.outstanding == database.units_in_flight() == 0
    return ledger


@pytest.mark.asyncio
async def test_graph_publication_measurement_matches_native_storage(
    database: Database,
    measurements_inputs: tuple[PreparedGraphPublicationBinds, ...],
    tmp_path: Path,
) -> None:
    await verify_measurement(database, measurements_inputs, "utf-8")
    if database.dialect is Dialect.POSTGRESQL:
        return
    for encoding in SQLITE_ENCODINGS:
        settings = encoded_sqlite_settings(tmp_path / f"{encoding}.db", encoding)
        await upgrade_database(settings)
        encoded = Database(settings)
        try:
            ledger = await verify_measurement(
                encoded, measurements_inputs, encoding.lower()
            )
        finally:
            await encoded.close()
        assert ledger.opened == ledger.closed > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", *SQLITE_ENCODINGS])
async def test_sqlite_record_measurement_matches_inserted_metadata(
    tmp_path: Path,
    measurements_inputs: tuple[PreparedGraphPublicationBinds, ...],
    encoding: str,
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "graph-record.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    ledger = ConnectionLedger(database)
    documents: tuple[JsonObject, ...] = (
        {},
        {"clé 😀": 'quote " and \\', "日本": "démo"},
    )
    actors = ("owner-ascii", "部署 😀")
    inputs = measurements_inputs[:2]
    parameters = tuple(
        item.parameters()
        | {
            RESOLVED_DIGESTS_PARAMETER: canonical_json(document),
            DEPLOYED_BY_PARAMETER: actor,
        }
        for item, document, actor in zip(inputs, documents, actors, strict=True)
    )
    measurement = sqlite_graph_publication_record_measurement_statement()
    values = graph_publication_bind_expressions(Dialect.SQLITE)
    metadata = sqlite_graph_publication_metadata_bind_expressions()
    fields = graph_version.c

    try:
        async with database.transaction() as writer:
            seeder = Seeder(writer)
            namespace_id = await seeder.namespace()
            graph_id = await seeder.graph(namespace_id)
            measured: list[tuple[int, ...]] = []
            positions: list[tuple[list[str] | None, list[str] | None]] = []
            captured: list[list[tuple[str, tuple[object, ...]]]] = []
            compiled_measurement = measurement.compile(dialect=writer.dialect)
            assert isinstance(compiled_measurement, SQLCompiler)
            assert all(
                compiled_measurement.binds[name].required for name in parameters[0]
            )
            for index, (item, document, actor, binds) in enumerate(
                zip(inputs, documents, actors, parameters, strict=True)
            ):
                common = dict(
                    graph_id=graph_id,
                    ir_digest=DIGEST,
                    source_digest=DIGEST,
                    execution_contract_digest=DIGEST,
                    deployment_digest=DIGEST,
                    value_encoding_version=1,
                    deployed_at=seeder.clock.now(),
                    status="active",
                    **seeder.stamps(),
                )
                proposed = (
                    insert(graph_version)
                    .values(**common, id=seeder.new_id(), number=2 * index + 1)
                    .values(values | metadata)
                )
                ordinary = (
                    insert(graph_version)
                    .values(
                        **common,
                        id=seeder.new_id(),
                        number=2 * index + 2,
                        resolved_digests=document,
                        deployed_by=actor,
                    )
                    .values(values)
                )
                compiled_insert = proposed.compile(dialect=writer.dialect)
                assert isinstance(compiled_insert, SQLCompiler)
                with executed_queries(writer) as operations:
                    measured.append(
                        tuple((await writer.execute(measurement, binds)).one())
                    )
                    await writer.execute(proposed, binds)
                captured.append(operations)
                positions.append(
                    (compiled_measurement.positiontup, compiled_insert.positiontup)
                )
                await writer.execute(ordinary, item.parameters())
        async with database.read_snapshot() as reader:
            stored = (
                await reader.execute(
                    select(
                        *(
                            sql_cast(fields[name], LargeBinary())
                            for name in (*values, *metadata)
                        )
                    )
                    .where(fields.graph_id == graph_id)
                    .order_by(fields.number)
                )
            ).all()
    finally:
        await database.close()

    assert tuple(measurement.selected_columns.keys()) == (
        *SIZE_COLUMNS,
        "resolved_digests_native_bytes",
        "deployed_by_native_bytes",
    )
    assert len(stored) == 4
    assert captured[0][0][0] == captured[1][0][0]
    for index, (binds, sizes, operations, bind_positions) in enumerate(
        zip(parameters, measured, captured, positions, strict=True)
    ):
        expected = tuple(value.encode(encoding) for value in binds.values())
        assert stored[2 * index] == stored[2 * index + 1] == expected
        ir, execution, resources, resolved, actor = map(len, expected)
        assert sizes == (ir, execution, resources, resources, resolved, actor)
        assert all(type(size) is int and size > 0 for size in sizes)
        assert len(operations) == len(bind_positions) == 2
        assert "graph_version" not in operations[0][0]
        for (sql, arguments), names in zip(operations, bind_positions, strict=True):
            assert names is not None
            assert set(binds) <= set(names)
            for name, argument in zip(names, arguments, strict=True):
                if name in binds:
                    assert argument is binds[name]
                    assert binds[name] not in sql
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.opened == ledger.closed > 0
