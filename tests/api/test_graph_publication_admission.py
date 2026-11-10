import asyncio
from datetime import UTC, datetime
from traceback import walk_tb
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.compiler import SQLCompiler

from kora.api.graph_publication_metadata import (
    PreparedGraphPublicationMetadata,
    prepare_graph_publication_metadata,
)
from kora.api.graph_publication_saved import (
    SAVED_LOCATOR,
    graph_publication_admission_statement,
    graph_publication_insert_statement,
    graph_publication_saved_statement,
)
from kora.api.graph_publication_values import (
    PreparedGraphPublicationBinds,
    graph_publication_measurement_statement,
    prepare_graph_publication_binds,
)
from kora.db.connection import Database, Dialect
from kora.db.graph_read_admission import (
    GraphVersionReadAdmission,
    read_graph_version_admission,
)
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import MAX_SQL_LENGTH
from kora.db.identifiers import encode
from kora.db.tables import graph_version, graph_version_read_admission
from kora.engine.graph_resource_references import GraphResourceScope
from tests.api.test_graph_publication_metadata import personal_principal
from tests.api.test_graph_publication_values import (
    checked_attachment,
    publication_limits,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries

STAMP = datetime(2026, 11, 10, 9, 8, 7, 654321, tzinfo=UTC)
SCOPE = GraphResourceScope(encode(1), encode(2), encode(3))


def prepare(scope: GraphResourceScope) -> PreparedGraphPublicationBinds:
    attachment = checked_attachment(with_files=False, scope=scope)
    assert attachment.owners == ()
    return prepare_graph_publication_binds(
        attachment, limits=publication_limits(attachment)
    )


def test_graph_publication_admission_rejects_invalid_scope_and_quantities() -> None:
    binds = prepare(SCOPE)
    malformed = prepare(
        GraphResourceScope(SCOPE.namespace_id, SCOPE.graph_id, "8" * 26)
    )
    exact = GraphVersionReadAdmission(MAX_SQL_LENGTH, 1)
    invalid = (
        GraphVersionReadAdmission(True, 1),
        GraphVersionReadAdmission(1, True),
        GraphVersionReadAdmission(0, 1),
        GraphVersionReadAdmission(1, -1),
        GraphVersionReadAdmission(MAX_SQL_LENGTH + 1, 1),
        GraphVersionReadAdmission(1, MAX_SQL_LENGTH + 1),
        GraphVersionReadAdmission(1, cast(int, cast(object, "private-input-1298"))),
        cast(GraphVersionReadAdmission, cast(object, (1, 2))),
    )

    native = binds.admission_values(exact)
    resource_maximum = binds.admission_values(
        GraphVersionReadAdmission(1, MAX_SQL_LENGTH)
    )
    for candidate, supplied in (
        *((binds, quantity) for quantity in invalid),
        (malformed, exact),
    ):
        with pytest.raises(GraphValueRejected) as refused:
            candidate.admission_values(supplied)

        assert refused.value.reason is GraphValueFailure.INVALID
        assert refused.value.__cause__ is refused.value.__context__ is None
        for frame, _ in walk_tb(refused.value.__traceback__):
            if frame.f_code.co_name == "admission_values":
                assert frame.f_locals == {}

    assert native == {
        "graph_version_id": SCOPE.version_id,
        "db_input_bytes": MAX_SQL_LENGTH,
        "resource_transport_bytes": 1,
    }
    assert resource_maximum == {
        "graph_version_id": SCOPE.version_id,
        "db_input_bytes": 1,
        "resource_transport_bytes": MAX_SQL_LENGTH,
    }


async def fixture_insert_pair(
    writer: AsyncConnection,
    dialect: Dialect,
    binds: PreparedGraphPublicationBinds,
    metadata: PreparedGraphPublicationMetadata,
    *,
    number: int,
) -> GraphVersionReadAdmission:
    insert = graph_publication_insert_statement(dialect)
    compare = graph_publication_saved_statement(dialect)
    witness = graph_publication_admission_statement(dialect)
    measurement = graph_publication_measurement_statement(dialect)
    payloads = binds.parameters()
    fixed = binds.fixed_values(dialect, number=number, source_digest="f" * 64, at=STAMP)
    version_values = fixed | payloads | metadata.parameters()

    native_width: object = (await writer.execute(measurement, payloads)).one()[3]
    assert type(native_width) is int
    locator: object = await writer.scalar(insert, version_values)
    matched: object = await writer.scalar(
        compare, version_values | {SAVED_LOCATOR: locator}
    )
    if dialect is Dialect.POSTGRESQL:
        assert matched is True
    else:
        assert type(matched) is int
        assert matched == 1
    admission = GraphVersionReadAdmission(MAX_SQL_LENGTH, native_width)
    parameters = binds.admission_values(admission)
    with executed_queries(writer) as queries:
        marker = tuple((await writer.execute(witness, parameters)).one())

    compiled = witness.compile(dialect=writer.dialect)
    assert isinstance(compiled, SQLCompiler)
    positions = compiled.positiontup
    assert positions is not None
    assert set(positions) == set(compiled.params) == set(parameters)
    assert all(compiled.binds[name].required for name in parameters)
    assert parameters["graph_version_id"] == fixed["id"]
    assert queries == [(compiled.string, tuple(parameters[name] for name in positions))]
    assert marker == (1,)
    assert type(marker[0]) is int
    if dialect is Dialect.SQLITE:
        assert witness.text.lstrip().startswith(
            "INSERT OR ABORT INTO main.graph_version_read_admission"
        )
    return admission


async def exact_pair(
    connection: AsyncConnection, version_id: str
) -> tuple[object, tuple[object, ...] | None]:
    version: object = await connection.scalar(
        select(graph_version.c.id).where(graph_version.c.id == version_id)
    )
    fields = graph_version_read_admission.c
    row = (
        await connection.execute(
            select(
                fields.format_version,
                fields.db_input_bytes,
                fields.resource_transport_bytes,
            ).where(fields.graph_version_id == version_id)
        )
    ).one_or_none()
    return version, None if row is None else tuple(row)


async def scoped_admission(
    connection: AsyncConnection, scope: GraphResourceScope
) -> GraphVersionReadAdmission | None:
    return await read_graph_version_admission(
        connection,
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        max_db_input_bytes=MAX_SQL_LENGTH,
        max_resource_transport_bytes=MAX_SQL_LENGTH,
    )


@pytest.mark.asyncio
async def test_graph_publication_admission_commits_and_rolls_back_with_exact_version(
    database: Database,
) -> None:
    dialect = database.dialect
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        other_namespace = await seeder.namespace("other")
        graph_id = await seeder.graph(namespace_id)
        first = GraphResourceScope(namespace_id, graph_id, seeder.new_id())
        rolled = GraphResourceScope(namespace_id, graph_id, seeder.new_id())
    first_binds = await asyncio.to_thread(prepare, first)
    rolled_binds = await asyncio.to_thread(prepare, rolled)
    metadata = prepare_graph_publication_metadata(
        dialect, personal_principal(), (), max_metadata_bytes=256
    )

    async with database.transaction() as writer:
        admission = await fixture_insert_pair(
            writer, dialect, first_binds, metadata, number=1
        )
        async with database.read_transaction() as reader:
            before_commit = await exact_pair(reader, first.version_id)
    with pytest.raises(IntegrityError):
        async with database.transaction() as writer:
            await writer.execute(
                graph_publication_admission_statement(dialect),
                first_binds.admission_values(GraphVersionReadAdmission(1, 1)),
            )
    async with database.transaction() as writer:
        rolled_admission = await fixture_insert_pair(
            writer, dialect, rolled_binds, metadata, number=2
        )
        native_before_rollback = await exact_pair(writer, rolled.version_id)
        await writer.rollback()
    async with database.read_transaction() as reader:
        after_commit = await exact_pair(reader, first.version_id)
        scoped = await scoped_admission(reader, first)
        wrong_scope = await scoped_admission(
            reader, GraphResourceScope(other_namespace, graph_id, first.version_id)
        )
        after_rollback = await exact_pair(reader, rolled.version_id)

    assert before_commit == after_rollback == (None, None)
    assert after_commit == (
        first.version_id,
        (1, MAX_SQL_LENGTH, admission.resource_transport_bytes),
    )
    assert native_before_rollback == (
        rolled.version_id,
        (1, MAX_SQL_LENGTH, rolled_admission.resource_transport_bytes),
    )
    assert scoped == admission
    assert wrong_scope is None
    assert admission.resource_transport_bytes > 1
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
