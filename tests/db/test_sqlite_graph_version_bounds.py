import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from sqlalchemy import Text, bindparam
from sqlalchemy.sql.compiler import SQLCompiler

from kora.api.graph_publication_sqlite import observe_sqlite_graph_publication
from kora.api.graph_publication_values import (
    DEPLOYED_BY_PARAMETER,
    RESOLVED_DIGESTS_PARAMETER,
    PreparedGraphPublicationBinds,
    graph_publication_bind_expressions,
    prepare_graph_publication_binds,
    sqlite_graph_publication_metadata_bind_expressions,
    sqlite_graph_publication_record_measurement_statement,
)
from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.migrations import upgrade_database
from kora.db.sqlite_graph_version_bounds import (
    SQLiteGraphVersionFootprint,
    SQLiteGraphVersionWidths,
    bound_sqlite_graph_version_record,
)
from kora.db.tables import graph_version
from kora.db.types import JsonObject, canonical_json
from tests.api.test_graph_publication_values import (
    checked_attachment,
    publication_limits,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_sqlite_graph_version_layout import PROBE_STATEMENTS
from tests.support.databases import sqlite_settings

SMALL_WIDTHS = SQLiteGraphVersionWidths(2, 2, 2, 2, 0)
RECORD_IMPLEMENTATION_MAX = 2_147_483_645
FIXTURE_PAGE_LIMIT = 2_048


@pytest.fixture(scope="module")
def record_publication_binds() -> tuple[
    PreparedGraphPublicationBinds, PreparedGraphPublicationBinds
]:
    without_files = checked_attachment(with_files=False)
    with_files = checked_attachment(with_files=True)
    return (
        prepare_graph_publication_binds(
            without_files, limits=publication_limits(without_files)
        ),
        prepare_graph_publication_binds(
            with_files, limits=publication_limits(with_files)
        ),
    )


def test_sqlite_graph_record_counts_fixed_fields_and_overflow_threshold() -> None:
    at_threshold = replace(SMALL_WIDTHS, ir_bytes=3_211)
    above_threshold = replace(at_threshold, ir_bytes=3_212)
    distinct_widths = SQLiteGraphVersionWidths(2, 4, 8, 16, 32)

    utf8 = bound_sqlite_graph_version_record(
        SMALL_WIDTHS, encoding="utf-8", page_size=4096
    )
    utf16 = tuple(
        bound_sqlite_graph_version_record(
            SMALL_WIDTHS, encoding=encoding, page_size=512
        )
        for encoding in ("utf-16le", "utf-16be")
    )
    inline = bound_sqlite_graph_version_record(
        at_threshold, encoding="utf-8", page_size=4096
    )
    overflow = bound_sqlite_graph_version_record(
        above_threshold, encoding="utf-8", page_size=4096
    )
    distinct = bound_sqlite_graph_version_record(
        distinct_widths, encoding="utf-8", page_size=4096
    )
    ordered = tuple(
        bound_sqlite_graph_version_record(
            replace(SMALL_WIDTHS, ir_bytes=width), encoding="utf-8", page_size=4096
        )
        for width in (2, 64, 3_211, 3_212, 10_000, 65_536)
    )

    assert utf8 == SQLiteGraphVersionFootprint(597, 4_096)
    assert utf16 == (SQLiteGraphVersionFootprint(1_008, 2_048),) * 2
    assert inline == SQLiteGraphVersionFootprint(3_806, 4_096)
    assert overflow == SQLiteGraphVersionFootprint(3_807, 8_192)
    assert distinct == SQLiteGraphVersionFootprint(651, 4_096)
    assert all(
        prior.record_payload_bytes < following.record_payload_bytes
        for prior, following in zip(ordered[:-1], ordered[1:], strict=True)
    )
    page_bytes = [estimate.leaf_and_overflow_bytes for estimate in ordered]
    assert page_bytes == sorted(page_bytes)


def test_sqlite_graph_record_caps_arithmetic_before_using_large_widths() -> None:
    maximum = replace(SMALL_WIDTHS, ir_bytes=RECORD_IMPLEMENTATION_MAX - 595)
    over = replace(maximum, ir_bytes=maximum.ir_bytes + 1)
    larger_than_a_record = replace(maximum, ir_bytes=1 << 200)

    accepted = bound_sqlite_graph_version_record(
        maximum, encoding="utf-8", page_size=65_536
    )
    with pytest.raises(GraphValueRejected) as above:
        bound_sqlite_graph_version_record(over, encoding="utf-8", page_size=65_536)
    with pytest.raises(GraphValueRejected) as oversized_field:
        bound_sqlite_graph_version_record(
            larger_than_a_record, encoding="utf-8", page_size=65_536
        )

    assert accepted == SQLiteGraphVersionFootprint(
        RECORD_IMPLEMENTATION_MAX, 32_900 * 65_536
    )
    assert above.value.reason is GraphValueFailure.BUDGET
    assert oversized_field.value.reason is GraphValueFailure.BUDGET


def test_sqlite_graph_record_refuses_malformed_layout_and_widths() -> None:
    malformed = (
        (SMALL_WIDTHS, "utf-8", True),
        (SMALL_WIDTHS, "utf-8", 511),
        (SMALL_WIDTHS, "utf-8", 1_025),
        (SMALL_WIDTHS, "utf-8", 131_072),
        (SMALL_WIDTHS, "ascii", 512),
        (replace(SMALL_WIDTHS, ir_bytes=True), "utf-8", 512),
        (replace(SMALL_WIDTHS, resources_bytes=0), "utf-8", 512),
        (replace(SMALL_WIDTHS, deployed_by_bytes=-1), "utf-8", 512),
        (replace(SMALL_WIDTHS, execution_contract_bytes=3), "utf-16be", 512),
        (cast(SQLiteGraphVersionWidths, (2, 2, 2, 2, 0)), "utf-8", 512),
    )

    refusals: list[GraphValueFailure] = []
    for widths, encoding, size in malformed:
        with pytest.raises(GraphValueRejected) as refused:
            bound_sqlite_graph_version_record(widths, encoding=encoding, page_size=size)
        refusals.append(refused.value.reason)

    assert refusals == [GraphValueFailure.INVALID] * len(malformed)


def native_varint(page: bytes, start: int) -> tuple[int, int]:
    value = 0
    for offset in range(8):
        byte = page[start + offset]
        value = (value << 7) | (byte & 0x7F)
        if byte < 128:
            return value, start + offset + 1
    return (value << 8) | page[start + 8], start + 9


def native_page(file: BinaryIO, number: int, size: int, count: int) -> bytes:
    assert 1 < number <= count
    file.seek((number - 1) * size)
    data = file.read(size)
    assert len(data) == size
    return data


def stored_record_footprint(
    path: Path, root: int, size: int
) -> SQLiteGraphVersionFootprint:
    page_count, remaining = divmod(path.stat().st_size, size)
    assert remaining == 0
    assert 1 < page_count <= FIXTURE_PAGE_LIMIT
    with path.open("rb") as file:
        header = file.read(100)
        assert header[:16] == b"SQLite format 3\0"
        size_code = int.from_bytes(header[16:18], "big")
        assert (65_536 if size_code == 1 else size_code) == size
        usable = size - header[20]
        leaf = native_page(file, root, size, page_count)
        assert leaf[0] == 0x0D
        assert int.from_bytes(leaf[3:5], "big") == 1
        cell = int.from_bytes(leaf[8:10], "big")
        record_bytes, rowid_offset = native_varint(leaf, cell)
        rowid, payload_offset = native_varint(leaf, rowid_offset)
        assert rowid > 0
        maximum_local = usable - 35
        if record_bytes <= maximum_local:
            return SQLiteGraphVersionFootprint(record_bytes, size)
        minimum_local = (usable - 12) * 32 // 255 - 23
        local = minimum_local + (record_bytes - minimum_local) % (usable - 4)
        pointer = payload_offset + (local if local <= maximum_local else minimum_local)
        page = int.from_bytes(leaf[pointer : pointer + 4], "big")
        assert page > 1
        visited = {root}
        while page:
            assert page not in visited
            visited.add(page)
            overflow = native_page(file, page, size, page_count)
            page = int.from_bytes(overflow[:4], "big")
    return SQLiteGraphVersionFootprint(record_bytes, len(visited) * size)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "page_size", "with_files"),
    [("UTF-8", 65_536, False), ("UTF-16le", 512, True), ("UTF-16be", 4_096, True)],
)
async def test_sqlite_graph_record_bound_covers_actual_migrated_file(
    tmp_path: Path,
    record_publication_binds: tuple[
        PreparedGraphPublicationBinds, PreparedGraphPublicationBinds
    ],
    encoding: str,
    page_size: int,
    with_files: bool,
) -> None:
    path = tmp_path / "record.db"
    with closing(sqlite3.connect(path)) as native:
        native.execute(f"PRAGMA page_size = {page_size}")
        native.execute(f"PRAGMA encoding = '{encoding}'")
        native.execute("CREATE TABLE encoding_seed (value INTEGER)")
        native.execute("DROP TABLE encoding_seed")
        native.commit()
    settings = sqlite_settings(path)
    await upgrade_database(settings)
    database = Database(settings)
    ledger = ConnectionLedger(database)
    binds = record_publication_binds[int(with_files)]
    resolved: JsonObject = {"café": "digest"}
    resolved_json = canonical_json(resolved)
    owner = "部署 😀"
    parameters = binds.parameters() | {
        RESOLVED_DIGESTS_PARAMETER: resolved_json,
        DEPLOYED_BY_PARAMETER: owner,
    }
    widths = SQLiteGraphVersionWidths(
        len(binds.ir_json.encode(encoding)),
        len(binds.execution_json.encode(encoding)),
        len(binds.resources_json.encode(encoding)),
        len(resolved_json.encode(encoding)),
        len(owner.encode(encoding)),
    )
    expected = bound_sqlite_graph_version_record(
        widths, encoding=encoding.lower(), page_size=page_size
    )
    max_metadata_bytes = len(resolved_json.encode()) + len(owner.encode())

    try:
        async with database.transaction() as writer:
            checkouts = ledger.checkouts
            compiled_measurement = (
                sqlite_graph_publication_record_measurement_statement().compile(
                    dialect=writer.dialect
                )
            )
            assert isinstance(compiled_measurement, SQLCompiler)
            with executed_queries(writer) as queries:
                observed = await observe_sqlite_graph_publication(
                    writer,
                    binds,
                    resolved_json,
                    owner,
                    max_metadata_bytes=max_metadata_bytes,
                )
            observation_checkouts = ledger.checkouts
            seeder = Seeder(writer)
            namespace_id = await seeder.namespace()
            graph_id = await seeder.graph(namespace_id)
            version_id = await seeder.graph_version(graph_id)
            await writer.execute(
                graph_version.update()
                .where(
                    graph_version.c.id
                    == bindparam("version_id", type_=Text(), required=True)
                )
                .values(graph_publication_bind_expressions(database.dialect))
                .values(sqlite_graph_publication_metadata_bind_expressions())
                .values(
                    status="retired",
                    value_encoding_version=1,
                ),
                parameters | {"version_id": version_id},
            )
    finally:
        await database.close()
    with closing(sqlite3.connect(path)) as native:
        checkpoint = native.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        saved_page = native.execute("PRAGMA page_size").fetchone()
        saved_encoding = native.execute("PRAGMA encoding").fetchone()
        root: object = native.execute(
            "SELECT rootpage FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'graph_version'"
        ).fetchone()[0]
        columns: tuple[str, ...] = tuple(
            row[1] for row in native.execute("PRAGMA table_info(graph_version)")
        )
    assert type(root) is int
    actual = stored_record_footprint(path, root, page_size)

    assert checkpoint[0] == 0
    assert saved_page == (page_size,)
    assert saved_encoding == (encoding,)
    assert len(queries) == 6
    assert queries[:-1] == [(statement.text, ()) for statement in PROBE_STATEMENTS]
    measurement_sql, measurement_args = queries[-1]
    assert measurement_sql == str(compiled_measurement)
    bind_names = compiled_measurement.positiontup
    assert bind_names is not None
    assert set(bind_names) == set(parameters)
    assert all(
        value is parameters[name]
        for name, value in zip(bind_names, measurement_args, strict=True)
    )
    assert all(value not in measurement_sql for value in parameters.values())
    assert observation_checkouts == checkouts
    assert observed.footprint == expected
    assert observed.resource_transport_bytes == len(
        binds.resources_json.encode(encoding)
    )
    assert len(columns) == 17
    assert set(columns) == set(graph_version.c.keys())
    assert 0 < actual.record_payload_bytes <= observed.footprint.record_payload_bytes
    assert actual.leaf_and_overflow_bytes <= observed.footprint.leaf_and_overflow_bytes
    assert (actual.leaf_and_overflow_bytes > page_size) is with_files
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.opened == ledger.closed > 0
