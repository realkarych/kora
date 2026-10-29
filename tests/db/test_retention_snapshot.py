import pytest
from pydantic import MISSING
from sqlalchemy import insert, literal_column, update
from sqlalchemy.exc import IntegrityError

from kora.db.connection import Database, Dialect
from kora.db.reads import retention_snapshot_by_run_id, run_by_id
from kora.db.retention import (
    INVALID_RETENTION,
    RetentionSnapshot,
    RetentionSnapshotError,
    UnversionedRetention,
    classify_retention_snapshot,
    resolve_retention_snapshot,
)
from kora.db.tables import namespace, run
from kora.db.types import JsonValue
from kora.ir.runtime import Retention
from tests.db.seed import Seeder
from tests.db.test_reads import seeded_graph


@pytest.mark.parametrize(
    ("graph", "namespace", "expected"),
    [
        ({}, {}, RetentionSnapshot("30d", "30d")),
        ({}, {"transcripts": "15d"}, RetentionSnapshot("15d", "30d")),
        (
            {"transcripts": "0d"},
            {"transcripts": "20d", "artifact_payloads": "forever"},
            RetentionSnapshot("0d", "forever"),
        ),
        (
            {"artifact_payloads": "forever"},
            {"transcripts": "0002d", "artifact_payloads": "3d"},
            RetentionSnapshot("2d", "forever"),
        ),
        (
            Retention(transcripts="000d"),
            {"artifact_payloads": "0d"},
            RetentionSnapshot("0d", "0d"),
        ),
        (
            {"transcripts": "000" + "9" * 5000 + "d"},
            {},
            RetentionSnapshot("9" * 5000 + "d", "30d"),
        ),
        (
            {"transcripts": "forever", "artifact_payloads": "0d"},
            None,
            RetentionSnapshot("forever", "0d"),
        ),
        (
            {"transcripts": "forever"},
            {"transcripts": None, "artifact_payloads": "5d"},
            RetentionSnapshot("forever", "5d"),
        ),
    ],
)
def test_retention_snapshot_resolves_fields_independently(
    graph: object, namespace: object, expected: RetentionSnapshot
) -> None:
    snapshot = resolve_retention_snapshot(graph, namespace)

    assert snapshot == expected
    assert resolve_retention_snapshot() == RetentionSnapshot("30d", "30d")


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        False,
        0,
        "",
        "1h",
        "-1d",
        "1.0d",
        "forever\n",
        [],
        ["1d"],
        {"private": "x"},
        MISSING,
    ],
)
@pytest.mark.parametrize("field", ["transcripts", "artifact_payloads"])
@pytest.mark.parametrize("layer", ["graph", "namespace"])
def test_retention_snapshot_rejects_selected_inputs(
    invalid: object, field: str, layer: str
) -> None:
    graph = {field: invalid} if layer == "graph" else {}
    namespace = {field: invalid} if layer == "namespace" else {field: "30d"}

    with pytest.raises(RetentionSnapshotError) as rejected:
        resolve_retention_snapshot(graph, namespace)

    assert str(rejected.value) == INVALID_RETENTION
    assert rejected.value.__cause__ is None


@pytest.mark.parametrize("invalid", [None, False, 0, "", [], ["30d"]])
def test_retention_snapshot_rejects_selected_containers(invalid: object) -> None:
    with pytest.raises(RetentionSnapshotError) as graph_error:
        resolve_retention_snapshot(invalid, {"transcripts": "2d"})
    with pytest.raises(RetentionSnapshotError) as namespace_error:
        resolve_retention_snapshot({}, invalid)

    assert str(graph_error.value) == str(namespace_error.value) == INVALID_RETENTION


def test_retention_snapshot_input_is_detached() -> None:
    graph = {"transcripts": "0002d"}
    namespace = {"artifact_payloads": "forever"}
    snapshot = resolve_retention_snapshot(graph, namespace)
    emitted = snapshot.to_json()

    graph["transcripts"] = "7d"
    namespace["artifact_payloads"] = "8d"
    emitted["transcripts"] = "9d"

    assert snapshot.to_json() == {"transcripts": "2d", "artifact_payloads": "forever"}


@pytest.mark.asyncio
async def test_retention_snapshot_round_trip_uses_recorded_values(
    database: Database,
) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    snapshot = resolve_retention_snapshot(
        {"transcripts": "2d"}, {"artifact_payloads": "forever"}
    )
    async with database.transaction() as connection:
        values = Seeder(connection).run_values(namespace_id, graph_id, version_id)
        values.update(retention=snapshot.to_json(), retention_version=1)
        await connection.execute(insert(run).values(**values))
        run_id = str(values["id"])

    async with database.transaction() as connection:
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(
                policy={"retention": {"transcripts": "0d", "artifact_payloads": "0d"}}
            )
        )
        await Seeder(connection).graph_version(
            graph_id, 2, {"runtime": {"retention": {"transcripts": "forever"}}}
        )
    async with database.read_transaction() as connection:
        recorded = await retention_snapshot_by_run_id(connection, namespace_id, run_id)
        row = await run_by_id(connection, namespace_id, run_id)

    assert recorded == snapshot
    assert row is not None
    assert row.retention == snapshot.to_json()
    assert row.retention_version == 1
    assert row.graph_version_id == version_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy",
    [
        {},
        {"transcripts": "0007d"},
        {"transcripts": "4d", "artifact_payloads": "forever"},
        None,
        ["old"],
    ],
)
async def test_retention_snapshot_legacy_rows_are_explicit(
    database: Database, legacy: JsonValue
) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    async with database.transaction() as connection:
        values = Seeder(connection).run_values(namespace_id, graph_id, version_id)
        values["retention"] = {} if legacy is None else legacy
        await connection.execute(insert(run).values(**values))
        run_id = str(values["id"])
        if legacy is None:
            null_json = (
                "'null'" if database.dialect is Dialect.SQLITE else "'null'::jsonb"
            )
            await connection.execute(
                update(run)
                .where(run.c.id == run_id)
                .values(retention=literal_column(null_json))
            )

    async with database.read_transaction() as connection:
        recorded = await retention_snapshot_by_run_id(connection, namespace_id, run_id)
        row = await run_by_id(connection, namespace_id, run_id)

    assert recorded == UnversionedRetention()
    assert row is not None
    assert row.retention == legacy
    assert row.retention_version is None


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {"transcripts": "1d"},
        {"transcripts": "1d", "artifact_payloads": "2d", "extra": "private"},
        {"transcripts": None, "artifact_payloads": "2d"},
        {"transcripts": 0, "artifact_payloads": "2d"},
        {"transcripts": "private", "artifact_payloads": "2d"},
        {"transcripts": "1d", "artifact_payloads": "02d"},
        {"transcripts": "00d", "artifact_payloads": "forever"},
        {"transcripts": "1d\n", "artifact_payloads": "forever"},
        [],
        None,
    ],
)
def test_retention_snapshot_rejects_malformed_tagged_values(malformed: object) -> None:
    with pytest.raises(RetentionSnapshotError) as rejected:
        classify_retention_snapshot(1, malformed)

    assert str(rejected.value) == INVALID_RETENTION


@pytest.mark.parametrize("version", [0, 2, True, "1", 1.0])
def test_retention_snapshot_rejects_unknown_versions(version: object) -> None:
    with pytest.raises(RetentionSnapshotError) as rejected:
        classify_retention_snapshot(version, RetentionSnapshot("30d", "30d").to_json())

    assert str(rejected.value) == INVALID_RETENTION


async def insert_unknown_version(database: Database, values: dict[str, object]) -> None:
    async with database.transaction() as connection:
        await connection.execute(insert(run).values(**values))


@pytest.mark.asyncio
async def test_retention_snapshot_schema_rejects_unknown_version(
    database: Database,
) -> None:
    namespace_id, graph_id, version_id = await seeded_graph(database)
    async with database.transaction() as connection:
        values = Seeder(connection).run_values(namespace_id, graph_id, version_id)
    values.update(retention_version=2, retention={})
    run_id = str(values["id"])

    with pytest.raises(IntegrityError):
        await insert_unknown_version(database, values)

    async with database.read_transaction() as connection:
        assert await run_by_id(connection, namespace_id, run_id) is None
