import asyncio
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import (
    Integer,
    LargeBinary,
    Text,
    cast,
    func,
    literal_column,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import TextClause

from kora.api.graph_publication_metadata import prepare_graph_publication_metadata
from kora.api.graph_publication_saved import (
    SAVED_LOCATOR,
    graph_publication_insert_statement,
    graph_publication_saved_statement,
)
from kora.api.graph_publication_values import (
    CONTRACT_PARAMETER,
    DEPLOYED_BY_PARAMETER,
    IR_PARAMETER,
    RESOLVED_DIGESTS_PARAMETER,
    RESOURCE_PARAMETER,
    graph_publication_bind_expressions,
    prepare_graph_publication_binds,
    sqlite_graph_publication_metadata_bind_expressions,
)
from kora.db.connection import Database, Dialect
from kora.db.identifiers import encode
from kora.db.migrations import upgrade_database
from kora.db.tables import event, graph_version, graph_version_read_admission
from kora.engine.graph_resource_references import GraphResourceScope
from tests.api.test_graph_publication_metadata import personal_principal
from tests.api.test_graph_publication_values import (
    PRIVATE_WIRE,
    checked_attachment,
    publication_limits,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.support.databases import copy_sqlite_template, sqlite_settings

STAMP = datetime(2026, 11, 10, 9, 8, 7, 654321, tzinfo=UTC)
ACTOR = "publication-owner"
RESOLVED = {"café": "a" * 64}
JSONB_SEMANTIC_MATCH = text("""
SELECT CAST(CAST(:original AS pg_catalog.text) AS pg_catalog.jsonb)
       OPERATOR(pg_catalog.=)
       CAST(CAST(:different_scale AS pg_catalog.text) AS pg_catalog.jsonb)
""")
SQLITE_BLOB_MATCH = text("""
SELECT typeof(resources), CAST(resources AS BLOB) = CAST(:expected AS BLOB)
FROM main.graph_version WHERE rowid = :locator
""")


async def representation_only_mismatch(
    writer: AsyncConnection,
    statement: TextClause,
    expected: dict[str, object],
    resources: str,
    sqlite: bool,
) -> None:
    if sqlite:
        mismatch: object = await writer.scalar(
            statement, expected | {RESOURCE_PARAMETER: " " + resources}
        )

        assert type(mismatch) is int
        assert mismatch == 0
        return
    different_scale, changes = re.subn(
        r'("total_size":)([0-9]+)', r"\g<1>\g<2>.0", resources, count=1
    )

    semantic: object = await writer.scalar(
        JSONB_SEMANTIC_MATCH,
        {"original": resources, "different_scale": different_scale},
    )
    mismatch = await writer.scalar(
        statement, expected | {RESOURCE_PARAMETER: different_scale}
    )

    assert changes == 1
    assert semantic is True
    assert mismatch is False


async def compare_fresh_graph_row(database: Database) -> None:
    dialect = database.dialect
    sqlite = dialect is Dialect.SQLITE
    ledger = ConnectionLedger(database)
    all_versions = select(graph_version).order_by(graph_version.c.number)
    witness_count = select(func.count()).select_from(graph_version_read_admission)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        scope = GraphResourceScope(
            namespace_id, await seeder.graph(namespace_id), seeder.new_id()
        )
        variant_id = seeder.new_id()
        conflict_id = seeder.new_id()
        await seeder.graph_version(scope.graph_id, number=2)
        prior = tuple((await writer.execute(all_versions)).one())
    candidate = await asyncio.to_thread(
        checked_attachment, with_files=True, scope=scope
    )
    payloads = prepare_graph_publication_binds(
        candidate, limits=publication_limits(candidate)
    )
    fixed = payloads.fixed_values(dialect, number=1, source_digest="f" * 64, at=STAMP)
    metadata = prepare_graph_publication_metadata(
        dialect,
        personal_principal(ACTOR),
        tuple(RESOLVED.items()),
        max_metadata_bytes=256,
    ).parameters()
    values = graph_publication_bind_expressions(dialect)
    proposed = graph_version.insert().values(fixed).values(values)
    inserts: dict[str, str] = payloads.parameters()
    if sqlite:
        proposed = proposed.values(sqlite_graph_publication_metadata_bind_expressions())
        inserts = inserts | metadata
        variant = proposed.values(
            id=variant_id, number=3, resources=cast(values["resources"], LargeBinary())
        )
    else:
        proposed = proposed.values(resolved_digests=RESOLVED, deployed_by=ACTOR)
        variant = proposed.values(id=variant_id, number=3, value_encoding_version=None)
    locator_column = (
        literal_column("rowid", type_=Integer())
        if sqlite
        else literal_column("CAST(ctid AS pg_catalog.text)", type_=Text())
    )
    insert = graph_publication_insert_statement(dialect)
    compare = graph_publication_saved_statement(dialect)
    insert_parameters: dict[str, object] = {
        **fixed,
        **payloads.parameters(),
        **metadata,
    }
    disagreements = {
        "id": encode(1),
        "graph_id": encode(2),
        "number": 2,
        "ir_digest": "2" * 64,
        "source_digest": "2" * 64,
        "execution_contract_digest": "2" * 64,
        "deployment_digest": "2" * 64,
        "deployed_at": STAMP + timedelta(microseconds=1),
        "created_at": STAMP + timedelta(microseconds=1),
        "updated_at": STAMP + timedelta(microseconds=1),
        "status": "retire",
        "value_encoding_version": 2,
        IR_PARAMETER: payloads.ir_json.replace(PRIVATE_WIRE, "b" * len(PRIVATE_WIRE)),
        CONTRACT_PARAMETER: '"changed execution contract"',
        RESOURCE_PARAMETER: "{}",
        RESOLVED_DIGESTS_PARAMETER: metadata[RESOLVED_DIGESTS_PARAMETER].replace(
            "a" * 64, "b" * 64
        ),
        DEPLOYED_BY_PARAMETER: ACTOR + "-部署",
    }

    async with database.transaction() as writer:
        compiled_insert = insert.compile(dialect=writer.dialect)
        compiled = compare.compile(dialect=writer.dialect)
        with executed_queries(writer) as operations:
            inserted = tuple((await writer.execute(insert, insert_parameters)).one())
            locator: object = inserted[0]
            expected = insert_parameters | {SAVED_LOCATOR: locator}
            marker = tuple((await writer.execute(compare, expected)).one())
        async with database.read_transaction() as reader:
            visible = tuple((await reader.execute(all_versions)).one())
            visible_admissions: object = await reader.scalar(witness_count)
        missing = (
            await writer.execute(
                compare, expected | {SAVED_LOCATOR: -1 if sqlite else "(0,100)"}
            )
        ).one_or_none()
        rejected: list[object] = [
            await writer.scalar(compare, expected | {name: value})
            for name, value in disagreements.items()
        ]
        await representation_only_mismatch(
            writer, compare, expected, payloads.resources_json, sqlite
        )
        variant_locator: object = await writer.scalar(
            variant.returning(locator_column), inserts
        )
        native_refusal: object = await writer.scalar(
            compare,
            expected | {"id": variant_id, "number": 3, SAVED_LOCATOR: variant_locator},
        )
        blob: tuple[object, ...] | None = None
        if sqlite:
            blob = tuple(
                (
                    await writer.execute(
                        SQLITE_BLOB_MATCH,
                        {
                            "locator": variant_locator,
                            "expected": payloads.resources_json,
                        },
                    )
                ).one()
            )
        raw_actor = "publication-部署"
        await writer.execute(
            graph_version.update()
            .where(graph_version.c.id == scope.version_id)
            .values(deployed_by=raw_actor)
        )
        raw_locator = await writer.scalar(
            select(locator_column)
            .select_from(graph_version)
            .where(graph_version.c.id == scope.version_id)
        )
        raw_expected = expected | {SAVED_LOCATOR: raw_locator}
        raw_match = await writer.scalar(
            compare, raw_expected | {DEPLOYED_BY_PARAMETER: raw_actor}
        )
        raw_mismatch = await writer.scalar(
            compare,
            raw_expected | {DEPLOYED_BY_PARAMETER: raw_actor.replace("署", "書")},
        )
        with pytest.raises(IntegrityError):
            await writer.execute(
                insert,
                insert_parameters
                | {
                    "id": conflict_id,
                    "number": 2,
                    DEPLOYED_BY_PARAMETER: "conflicting",
                },
            )
        await writer.rollback()
    async with database.read_transaction() as reader:
        remaining = tuple((await reader.execute(all_versions)).one())
        admissions: object = await reader.scalar(witness_count)

    assert len(inserted) == len(marker) == 1
    assert len(operations) == 2
    assert type(locator) is (int if sqlite else str)
    if isinstance(locator, str):
        assert re.fullmatch(r"\([0-9]{1,10},[0-9]{1,5}\)", locator) is not None
    assert marker == ((1,) if sqlite else (True,))
    assert type(marker[0]) is (int if sqlite else bool)
    assert missing is None
    assert rejected == [0 if sqlite else False] * 17
    assert raw_match == (1 if sqlite else True)
    assert raw_mismatch == (0 if sqlite else False)
    assert native_refusal == (0 if sqlite else False)
    assert blob == (("blob", 1) if sqlite else None)
    assert isinstance(compiled_insert, SQLCompiler)
    assert isinstance(compiled, SQLCompiler)
    assert set(compiled_insert.params) == set(insert_parameters)
    assert all(compiled_insert.binds[name].required for name in insert_parameters)
    assert set(compiled.params) == set(expected)
    assert all(compiled.binds[name].required for name in expected)
    assert len(expected) == 18
    (insert_sql, insert_args), (compare_sql, compare_args) = operations
    assert len(insert_args) == 17
    assert len(compare_args) == 18
    insert_order = compiled_insert.positiontup
    compare_order = compiled.positiontup
    assert insert_order is not None
    assert compare_order is not None
    for name, value in metadata.items():
        assert insert_args[insert_order.index(name)] is value
        assert compare_args[compare_order.index(name)] is value
    for sql in (insert_sql, compare_sql):
        assert PRIVATE_WIRE not in sql
        assert all(value not in sql for value in payloads.parameters().values())
    assert visible == remaining == prior
    assert visible_admissions == admissions == 0
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_publication_compares_fresh_saved_values(database: Database) -> None:
    await compare_fresh_graph_row(database)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
async def test_publication_compares_saved_utf16_bytes(
    tmp_path: Path, encoding: str
) -> None:
    path = tmp_path / "saved-value.db"
    with closing(sqlite3.connect(path)) as native:
        native.execute(f"PRAGMA encoding = '{encoding}'")
        native.execute("CREATE TABLE encoding_seed (value INTEGER)")
        native.execute("DROP TABLE encoding_seed")
        native.commit()
    settings = sqlite_settings(path)
    await upgrade_database(settings)
    database = Database(settings)
    try:
        await compare_fresh_graph_row(database)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_sqlite_later_event_trigger_invalidates_the_saved_match(
    tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "publication.db")
    database = Database(settings)
    ledger = ConnectionLedger(database)
    insert = graph_publication_insert_statement(Dialect.SQLITE)
    compare = graph_publication_saved_statement(Dialect.SQLITE)
    rewrite_on_event = text("""
        CREATE TEMP TRIGGER publication_event_probe AFTER INSERT ON main.event
        BEGIN
            UPDATE graph_version
            SET source_digest = replace(source_digest, 'f', 'e')
            WHERE id = NEW.id;
        END
    """)

    try:
        async with database.transaction() as writer:
            seeder = Seeder(writer)
            namespace_id = await seeder.namespace()
            scope = GraphResourceScope(
                namespace_id, await seeder.graph(namespace_id), seeder.new_id()
            )
        candidate = await asyncio.to_thread(
            checked_attachment, with_files=False, scope=scope
        )
        payloads = prepare_graph_publication_binds(
            candidate, limits=publication_limits(candidate)
        )
        fixed = payloads.fixed_values(
            Dialect.SQLITE, number=1, source_digest="f" * 64, at=STAMP
        )
        metadata = prepare_graph_publication_metadata(
            Dialect.SQLITE,
            personal_principal(ACTOR),
            tuple(RESOLVED.items()),
            max_metadata_bytes=256,
        )
        parameters = fixed | payloads.parameters() | metadata.parameters()

        async with database.transaction() as writer:
            await writer.execute(rewrite_on_event)
            locator: object = await writer.scalar(insert, parameters)
            expected = parameters | {SAVED_LOCATOR: locator}
            original: object = await writer.scalar(compare, expected)
            await writer.execute(
                event.insert().values(
                    id=scope.version_id,
                    namespace_id=scope.namespace_id,
                    kind="fixture.publication",
                    actor={"kind": "system"},
                    payload={},
                    at=STAMP,
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
            changed: object = await writer.scalar(compare, expected)
            rewritten = tuple(
                (
                    await writer.execute(
                        select(
                            literal_column("rowid", type_=Integer()),
                            cast(graph_version.c.source_digest, LargeBinary()),
                        ).where(graph_version.c.id == scope.version_id)
                    )
                ).one()
            )
            await writer.rollback()
        async with database.transaction() as writer:
            versions = await writer.scalar(
                select(func.count()).select_from(graph_version)
            )
            events = await writer.scalar(select(func.count()).select_from(event))
            admissions = await writer.scalar(
                select(func.count()).select_from(graph_version_read_admission)
            )
    finally:
        await database.close()

    assert type(locator) is int
    assert type(original) is int
    assert original == 1
    assert type(changed) is int
    assert changed == 0
    assert rewritten == (locator, b"e" * 64)
    assert versions == events == admissions == 0
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    assert ledger.opened == ledger.closed > 0


@pytest.mark.asyncio
async def test_sqlite_graph_version_insert_aborts_a_schema_replacement_conflict(
    tmp_path: Path, sqlite_template: Path
) -> None:
    database = Database(copy_sqlite_template(sqlite_template, tmp_path / "conflict.db"))
    ledger = ConnectionLedger(database)
    insert = graph_publication_insert_statement(Dialect.SQLITE)
    compare = graph_publication_saved_statement(Dialect.SQLITE)
    definition = text(
        "SELECT sql FROM main.sqlite_schema "
        "WHERE type = 'table' AND name = 'graph_version'"
    )
    versions = select(graph_version.c.id)
    admission_count = select(func.count()).select_from(graph_version_read_admission)
    ordinal = "UNIQUE (graph_id, number)"
    metadata = prepare_graph_publication_metadata(
        Dialect.SQLITE, personal_principal(ACTOR), (), max_metadata_bytes=256
    )
    prepared: list[dict[str, str | int | datetime]] = []

    try:
        async with database.transaction() as writer:
            seeder = Seeder(writer)
            namespace_id = await seeder.namespace()
            graph_id = await seeder.graph(namespace_id)
            scopes = tuple(
                GraphResourceScope(namespace_id, graph_id, seeder.new_id())
                for _ in range(2)
            )
        for scope, digest in zip(scopes, ("a" * 64, "b" * 64), strict=True):
            candidate = await asyncio.to_thread(
                checked_attachment, with_files=False, scope=scope
            )
            payloads = prepare_graph_publication_binds(
                candidate, limits=publication_limits(candidate)
            )
            fixed = payloads.fixed_values(
                Dialect.SQLITE, number=1, source_digest=digest, at=STAMP
            )
            prepared.append(fixed | payloads.parameters() | metadata.parameters())
        original, conflict = prepared

        async with database.transaction() as writer:
            migrated: object = await writer.scalar(definition)
            assert type(migrated) is str
            assert migrated.count(ordinal) == 1
            await writer.exec_driver_sql(
                "ALTER TABLE main.graph_version RENAME TO previous_graph_version"
            )
            await writer.exec_driver_sql(
                migrated.replace(ordinal, ordinal + " ON CONFLICT REPLACE")
            )
            locator: object = await writer.scalar(insert, original)
            with pytest.raises(IntegrityError) as refused:
                await writer.scalar(insert, conflict)
            matched: object = await writer.scalar(
                compare, original | {SAVED_LOCATOR: locator}
            )
            surviving = (await writer.execute(versions)).scalars().all()
            await writer.rollback()
        async with database.transaction() as writer:
            restored: object = await writer.scalar(definition)
            remaining = (await writer.execute(versions)).all()
            admissions: object = await writer.scalar(admission_count)
    finally:
        await database.close()

    native: object = refused.value.orig
    assert isinstance(native, sqlite3.IntegrityError)
    assert native.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE
    assert type(locator) is int
    assert type(matched) is int
    assert matched == 1
    assert surviving == [scopes[0].version_id]
    assert restored == migrated
    assert remaining == []
    assert admissions == 0
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    assert ledger.opened == ledger.closed > 0
