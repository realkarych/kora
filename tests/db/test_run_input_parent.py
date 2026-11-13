import asyncio
import base64
import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import Connection, event, func, insert, select, update
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util.concurrency import await_

from kora.db.connection import Database, Dialect
from kora.db.identifiers import encode
from kora.db.migrations import upgrade_database
from kora.db.run_input_parent import INPUT_SENTINEL, verify_run_input_parent
from kora.db.tables import run
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
)
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.db.test_sqlite_writer_entry import writer_driver
from tests.support.databases import encoded_sqlite_settings, sqlite_settings

PARENT_ALLOWANCE: int = 4_096
PRIVATE: str = "private-run-input-parent-fixture"
SQLITE_PARENT = "FROM main.run INDEXED BY ix_run_input_locator"
POSTGRES_PARENT = "FROM run "
DROP_PARENT_INDEX = "DROP INDEX main.ix_run_input_locator"
SQLITE_COLUMN_TYPE = 0x80
SQLITE_COLUMN_WIDTH = 0xC0


async def open_sqlite(tmp_path: Path, encoding: str = "UTF-8") -> Database:
    settings = encoded_sqlite_settings(tmp_path / "run-inputs.db", encoding)
    await upgrade_database(settings)
    return Database(narrow_settings(settings, pool_size=1))


def is_parent_query(statement: str) -> bool:
    normalized = " ".join(statement.split())
    return SQLITE_PARENT in normalized or POSTGRES_PARENT in normalized


@dataclass(frozen=True)
class Binding:
    namespace_id: str
    graph_id: str
    version_id: str
    run_id: str

    async def verify(
        self, connection: AsyncConnection, allowance: int = PARENT_ALLOWANCE
    ) -> bool:
        return await verify_run_input_parent(
            connection,
            self.namespace_id,
            self.graph_id,
            self.version_id,
            self.run_id,
            max_parent_bytes=allowance,
        )


async def seed(
    database: Database, overrides: dict[str, object] | None = None
) -> Binding:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
        values = seeder.run_values(namespace_id, graph_id, version_id)
        values.update(
            input_encoding_version=1,
            params=INPUT_SENTINEL,
            cel_params_projection=INPUT_SENTINEL,
        )
        if overrides is not None:
            values.update(overrides)
        await connection.execute(insert(run).values(**values))
    return Binding(namespace_id, graph_id, version_id, str(values["id"]))


async def read_parent(database: Database, binding: Binding) -> bool:
    async with database.read_snapshot() as reader:
        verified = await binding.verify(reader)
        if not verified:
            await reader.rollback()
        return verified


def assert_sqlite_parent_guards_value_reads(
    instructions: list[tuple[object, ...]], arguments: tuple[object, ...]
) -> None:
    [allowance] = [
        instruction[3]
        for instruction in instructions
        if instruction[1] == "Variable"
        and type(instruction[2]) is int
        and arguments[instruction[2] - 1] == PARENT_ALLOWANCE
    ]
    [guard] = [
        instruction
        for instruction in instructions
        if instruction[1] == "Gt" and instruction[2] == allowance
    ]
    address, skip_values = guard[0], guard[3]
    assert type(address) is int
    assert type(skip_values) is int
    assert instructions[skip_values][1:3] == ("Integer", 0)
    columns = run.c.keys()
    for field in (
        "params",
        "cel_params_projection",
        "trigger",
        "input_encoding_version",
    ):
        position = columns.index(field)
        reads = [
            instruction
            for instruction in instructions
            if instruction[1] == "Column" and instruction[3] == position
        ]
        metadata = [instruction[6] for instruction in reads[:3]]
        assert metadata == [SQLITE_COLUMN_TYPE, SQLITE_COLUMN_TYPE, SQLITE_COLUMN_WIDTH]
        for read in reads:
            if read[6] == 0:
                read_address = read[0]
                assert type(read_address) is int
                assert address < read_address < skip_values


@pytest.mark.asyncio
async def test_run_input_parent_refuses_native_lock_timeout(
    database: Database,
) -> None:
    binding = await seed(database)
    await database.close()
    settings = database.settings
    if settings.dialect is Dialect.POSTGRESQL:
        settings = replace(
            settings, url=settings.url.update_query_dict({"command_timeout": "1.0"})
        )
    probe = Database(settings)
    ledger = ConnectionLedger(probe)
    try:
        if probe.dialect is Dialect.POSTGRESQL:
            async with probe.transaction() as writer:
                await writer.exec_driver_sql("LOCK TABLE run IN ACCESS EXCLUSIVE MODE")
                refused = await read_parent(probe, binding)
        else:
            with closing(sqlite3.connect(settings.sqlite_path)) as writer:
                writer.execute("PRAGMA journal_mode=DELETE")
                async with probe.read_transaction() as reader:
                    await reader.exec_driver_sql("PRAGMA busy_timeout=100")
                writer.execute("BEGIN EXCLUSIVE")
                try:
                    refused = await read_parent(probe, binding)
                finally:
                    writer.rollback()
        recovered = await read_parent(probe, binding)
    finally:
        await probe.close()

    assert (refused, recovered) == (False, True)
    assert ledger.outstanding == probe.units_in_flight() == 0
    assert ledger.opened == ledger.closed > 0


@pytest.mark.asyncio
async def test_run_input_parent_requires_exact_tagged_scope(database: Database) -> None:
    binding = await seed(database)
    ledger = ConnectionLedger(database)
    incorrect = (
        replace(binding, namespace_id=binding.run_id),
        replace(binding, graph_id=binding.run_id),
        replace(binding, version_id=binding.run_id),
        replace(binding, run_id=binding.version_id),
        replace(binding, run_id=PRIVATE),
    )

    unused = await binding.verify(database.reader_engine.connect())
    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            confirmed = await binding.verify(connection)
            refused = [await other.verify(connection) for other in incorrect]
            bad_budget = await binding.verify(connection, 0)
    after_release = await binding.verify(connection)
    parent_positions = [
        position for position, (sql, _) in enumerate(queries) if is_parent_query(sql)
    ]

    assert confirmed is True
    assert refused == [False] * len(incorrect)
    assert unused is bad_budget is after_release is False
    assert ledger.checkouts == 1
    assert ledger.outstanding == database.units_in_flight() == 0
    assert len(parent_positions) == 5
    assert all(
        "FROM run_inputs" not in sql and "FROM main.run_inputs" not in sql
        for sql, _ in queries
    )
    assert len(queries) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"input_encoding_version": None}, False),
        ({"input_encoding_version": 2}, False),
        ({"params": {"marker": INPUT_SENTINEL}}, False),
        ({"cel_params_projection": [INPUT_SENTINEL]}, False),
        ({"params": PRIVATE}, False),
        ({}, True),
    ],
    ids=[
        "legacy",
        "unsupported",
        "wrong-raw-type",
        "wrong-cut-type",
        "conflict",
        "valid",
    ],
)
async def test_run_input_parent_does_not_substitute_markers(
    database: Database, overrides: dict[str, object], expected: bool
) -> None:
    binding = await seed(database, overrides)

    async with database.read_snapshot() as connection:
        verified = await binding.verify(connection)
        old_inline: object = await connection.scalar(
            select(run.c.params).where(run.c.id == binding.run_id)
        )

    assert verified is expected
    assert old_inline == overrides.get("params", INPUT_SENTINEL)


@pytest.mark.asyncio
async def test_run_input_parent_rechecks_in_the_callers_snapshot(
    database: Database,
) -> None:
    binding = await seed(database)
    ledger = ConnectionLedger(database)

    async with database.read_snapshot() as connection:
        before = await binding.verify(connection)
        async with database.transaction() as writer:
            await writer.execute(
                update(run).where(run.c.id == binding.run_id).values(params=PRIVATE)
            )
        during = await binding.verify(connection)
    async with database.read_snapshot() as fresh:
        after = await binding.verify(fresh)

    assert (before, during, after) == (True, True, False)
    assert ledger.checkouts == 3
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["params", "cel_params_projection"])
@pytest.mark.parametrize("compressible", [True, False], ids=["compressed", "external"])
async def test_run_input_parent_refuses_wide_inline_storage(
    database: Database,
    column: Literal["params", "cel_params_projection"],
    compressible: bool,
) -> None:
    wide = (
        "a" * 65_536
        if compressible
        else base64.b64encode(
            hashlib.shake_256(b"external input").digest(65_536)
        ).decode()
    )
    binding = await seed(database, {column: wide})

    async with database.read_snapshot() as connection:
        if database.dialect is Dialect.POSTGRESQL:
            field = run.c[column]
            raw = await connection.execute(
                select(
                    func.pg_column_size(field), func.pg_column_compression(field)
                ).where(run.c.id == binding.run_id)
            )
            physical, compression = tuple(raw.one())
        else:
            physical, compression = None, None
        verified = await binding.verify(connection)

    assert verified is False
    if database.dialect is Dialect.POSTGRESQL:
        assert type(physical) is int
        if compressible:
            assert compression is not None
            assert physical < len(wide)
        else:
            assert compression is None
            assert physical > 65_536


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "trigger"),
    [(1, "x" * 65_536), (2, "x" * 65_536), (1, "é" * 2_500)],
    ids=["valid-tag", "unsupported-tag", "multibyte"],
)
async def test_run_input_parent_applies_sqlite_bound_before_late_tag(
    database: Database, tag: int, trigger: str
) -> None:
    binding = await seed(database, {"trigger": trigger, "input_encoding_version": tag})

    async with database.read_snapshot() as connection:
        within_small_bound = await binding.verify(connection)
        within_large_bound = await binding.verify(connection, 131_072)

    assert within_small_bound is (database.dialect is Dialect.POSTGRESQL and tag == 1)
    assert within_large_bound is (tag == 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16le", "utf-16be"])
async def test_run_input_parent_compares_decoded_sqlite_markers_in_native_widths(
    tmp_path: Path, encoding: str
) -> None:
    path = tmp_path / "native-width.sqlite"
    with closing(sqlite3.connect(path)) as raw:
        raw.execute(f"PRAGMA encoding = '{encoding}'")
        raw.execute("CREATE TABLE input_encoding_probe (value INTEGER)")
        raw.execute("DROP TABLE input_encoding_probe")
    settings = sqlite_settings(path)
    await upgrade_database(settings)
    database = Database(settings)
    try:
        binding = await seed(database)
        async with database.read_snapshot() as connection:
            selected: object = (
                await connection.exec_driver_sql("PRAGMA encoding")
            ).scalar_one()
        encoded = (
            r'"\u006bora.run-inputs.v1"',
            r'"kora.run-inputs.v1\u0000x"',
            '"kora.run-inputs.v1"\x00ignored',
            b"{malformed-marker",
        )

        checked: list[bool] = []
        for marker in encoded:
            async with database.transaction() as writer:
                await writer.exec_driver_sql(
                    "UPDATE run SET params=? WHERE id=?", (marker, binding.run_id)
                )
            async with database.read_snapshot() as connection:
                checked.append(await binding.verify(connection))
    finally:
        await database.close()

    assert type(selected) is str
    assert selected.lower() == encoding
    assert checked == [True, False, False, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_run_input_parent_uses_the_partial_locator_next_to_legacy_keys(
    tmp_path: Path, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    try:
        binding = await seed(database)
        missing = replace(binding, run_id=encode(0))
        async with database.transaction() as writer:
            for key in ("0" * (4 * 1024 * 1024), b"0" * (4 * 1024 * 1024)):
                run_id = await Seeder(writer).run(
                    binding.namespace_id, binding.graph_id, binding.version_id
                )
                await writer.exec_driver_sql(
                    "UPDATE main.run SET id = ? WHERE id = ?", (key, run_id)
                )

        async with database.read_snapshot() as reader:
            with executed_queries(reader) as queries:
                found = await binding.verify(reader)
                absent = await missing.verify(reader)
            lookups = [query for query in queries if is_parent_query(query[0])]
            plans = [
                [
                    tuple(row)[3]
                    for row in await reader.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + statement, arguments
                    )
                ]
                for statement, arguments in lookups
            ]
            statement, arguments = lookups[0]
            instructions = await sqlite_instructions(reader, statement, arguments)

        assert (found, absent) == (True, False)
        assert len(plans) == 2
        for plan in plans:
            assert len(plan) == 1
            detail: object = plan[0]
            assert isinstance(detail, str)
            assert detail.startswith("SEARCH ")
            assert detail.endswith("USING INDEX ix_run_input_locator (id=?)")
        assert_sqlite_parent_guards_value_reads(instructions, arguments)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_parent_refuses_missing_main_index_on_the_fresh_snapshot(
    tmp_path: Path,
) -> None:
    database = await open_sqlite(tmp_path)
    try:
        binding = await seed(database)
        async with database.reader_engine.connect() as setup:
            await setup.exec_driver_sql("PRAGMA query_only = 0")
            await setup.exec_driver_sql("CREATE TABLE temp.run (wrong)")
            await setup.exec_driver_sql(
                "CREATE INDEX temp.ix_run_input_locator ON run(wrong)"
            )
            await setup.exec_driver_sql("PRAGMA query_only = 1")
            await setup.commit()
            original_driver = await writer_driver(setup)

        async with database.read_snapshot() as reader:
            snapshot_driver = await writer_driver(reader)
            before = await binding.verify(reader)
            async with database.transaction() as writer:
                await writer.exec_driver_sql(DROP_PARENT_INDEX)
            during = await binding.verify(reader)
        async with database.read_snapshot() as fresh:
            fresh_driver = await writer_driver(fresh)
            with executed_queries(fresh) as queries:
                after = await binding.verify(fresh)

        assert snapshot_driver is fresh_driver is original_driver
        assert (before, during, after) == (True, True, False)
        assert queries
        assert sum(is_parent_query(statement) for statement, _ in queries) == 1
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_parent_cancellation_releases_snapshot(
    database: Database,
) -> None:
    binding = await seed(database)
    ledger = ConnectionLedger(database)
    dispatched = asyncio.Event()
    paused = asyncio.Event()

    def hold(
        connection: Connection, cursor: DBAPICursor, statement: str, *rest: object
    ) -> None:
        if is_parent_query(statement):
            dispatched.set()
            await_(paused.wait())

    async def observe() -> bool:
        async with database.read_snapshot() as connection:
            event.listen(connection.sync_connection, "after_cursor_execute", hold)
            return await binding.verify(connection)

    task = asyncio.create_task(observe())
    try:
        await asyncio.wait_for(dispatched.wait(), CONDITION_DEADLINE_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
    finally:
        paused.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with database.read_snapshot() as connection:
        after = await binding.verify(connection)

    assert after is True
    assert ledger.checkouts == 2
    assert ledger.outstanding == database.units_in_flight() == 0
