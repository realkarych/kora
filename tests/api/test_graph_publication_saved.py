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
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import TextClause

from kora.api.graph_publication_saved import (
    SAVED_LOCATOR,
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
from kora.db.tables import graph_version, graph_version_read_admission
from kora.db.types import JsonObject, canonical_json
from kora.engine.graph_resource_references import GraphResourceScope
from tests.api.test_graph_publication_values import (
    PRIVATE_WIRE,
    checked_attachment,
    publication_limits,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.support.databases import sqlite_settings

STAMP = datetime(2026, 11, 10, 9, 8, 7, 654321, tzinfo=UTC)
ACTOR = "publication-部署"
RESOLVED: JsonObject = {"café": "a" * 64}
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
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        scope = GraphResourceScope(
            namespace_id, await seeder.graph(namespace_id), seeder.new_id()
        )
        variant_id = seeder.new_id()
    candidate = await asyncio.to_thread(
        checked_attachment, with_files=True, scope=scope
    )
    payloads = prepare_graph_publication_binds(
        candidate, limits=publication_limits(candidate)
    )
    fixed = payloads.fixed_values(dialect, number=1, source_digest="f" * 64, at=STAMP)
    metadata = {
        RESOLVED_DIGESTS_PARAMETER: canonical_json(RESOLVED),
        DEPLOYED_BY_PARAMETER: ACTOR,
    }
    values = graph_publication_bind_expressions(dialect)
    proposed = graph_version.insert().values(fixed).values(values)
    inserts: dict[str, str] = payloads.parameters()
    if sqlite:
        proposed = proposed.values(sqlite_graph_publication_metadata_bind_expressions())
        inserts = inserts | metadata
        variant = proposed.values(
            id=variant_id, number=2, resources=cast(values["resources"], LargeBinary())
        )
    else:
        proposed = proposed.values(resolved_digests=RESOLVED, deployed_by=ACTOR)
        variant = proposed.values(id=variant_id, number=2, value_encoding_version=None)
    locator_column = (
        literal_column("rowid", type_=Integer())
        if sqlite
        else literal_column("CAST(ctid AS pg_catalog.text)", type_=Text())
    )
    compare = graph_publication_saved_statement(dialect)
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
        DEPLOYED_BY_PARAMETER: ACTOR.replace("署", "書"),
    }

    async with database.transaction() as writer:
        locator: object = await writer.scalar(
            proposed.returning(locator_column), inserts
        )
        expected: dict[str, object] = (
            fixed
            | payloads.parameters()
            | metadata
            | {
                SAVED_LOCATOR: locator,
            }
        )
        compiled = compare.compile(dialect=writer.dialect)
        with executed_queries(writer) as operations:
            marker = tuple((await writer.execute(compare, expected)).one())
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
            expected | {"id": variant_id, "number": 2, SAVED_LOCATOR: variant_locator},
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
        await writer.rollback()
    async with database.read_transaction() as reader:
        versions: object = await reader.scalar(
            select(func.count()).select_from(graph_version)
        )
        admissions: object = await reader.scalar(
            select(func.count()).select_from(graph_version_read_admission)
        )

    assert len(marker) == len(operations) == 1
    assert type(locator) is (int if sqlite else str)
    assert marker == ((1,) if sqlite else (True,))
    assert type(marker[0]) is (int if sqlite else bool)
    assert missing is None
    assert rejected == [0 if sqlite else False] * 17
    assert native_refusal == (0 if sqlite else False)
    assert blob == (("blob", 1) if sqlite else None)
    assert isinstance(compiled, SQLCompiler)
    assert set(compiled.params) == set(expected)
    assert all(compiled.binds[name].required for name in expected)
    assert len(expected) == 18
    sql, parameters = operations[0]
    assert len(parameters) == 18
    assert PRIVATE_WIRE not in sql
    assert all(value not in sql for value in payloads.parameters().values())
    assert versions == admissions == 0
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
