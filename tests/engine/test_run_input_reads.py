import asyncio
import base64
import hashlib
import math
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import Connection, event, func, insert, select, update
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.util.concurrency import await_

from kora.db.connection import Database, Dialect
from kora.db.migrations import upgrade_database
from kora.db.run_input_reads import (
    RunInputBinding,
    RunInputField,
    RunInputReadLimits,
    run_input_value_statement,
)
from kora.db.tables import run, run_inputs
from kora.engine.run_input_reads import read_run_input
from kora.engine.start_records import MAX_PARAMS_BYTES
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import sqlite_instructions
from tests.db.test_run_input_parent import is_parent_query, open_sqlite
from tests.db.test_run_input_parent import seed as seed_parent
from tests.engine.test_run_input_values import CANONICAL
from tests.support.databases import sqlite_settings

PROJECTION = b'{"cut":1}'
LIMITS = RunInputReadLimits(4096, 4096, MAX_PARAMS_BYTES)
PRIVATE: str = "private-run-input-read-fixture"


def is_side_query(statement: str) -> bool:
    return (
        "FROM run_inputs" in statement or "FROM main.run_inputs INDEXED BY" in statement
    )


def assert_sqlite_side_guards_value_reads(
    instructions: list[tuple[object, ...]], field: RunInputField
) -> None:
    projection = [row for row in instructions if row[1] == "Column" and row[3] == 1]
    raw = [row for row in instructions if row[1] == "Column" and row[3] == 2]
    expected_projection = (
        [0x80, 0x40, 0] if field is RunInputField.PROJECTION else [0x80, 0x40]
    )
    assert [row[6] for row in projection] == expected_projection
    assert [row[6] for row in raw] == (
        [] if field is RunInputField.PROJECTION else [0x80, 0x40, 0]
    )
    for row in projection + raw:
        if row[6] == 0:
            address = row[0]
            assert type(address) is int
            guard = instructions[address - 1]
            assert guard[1] == "IfNot"
            assert type(guard[3]) is int
            assert guard[3] > address
    if field is RunInputField.PARAMS:
        first_raw = raw[0][0]
        last_raw = raw[-1][0]
        assert type(first_raw) is int
        assert type(last_raw) is int
        predecessor_guard = instructions[first_raw - 1]
        assert predecessor_guard[1] == "IfNot"
        assert type(predecessor_guard[3]) is int
        assert predecessor_guard[3] > last_raw


async def seed(
    database: Database,
    *,
    projection: bytes = PROJECTION,
    params: bytes = CANONICAL,
    side_row: bool = True,
) -> RunInputBinding:
    parent = await seed_parent(database)
    if side_row:
        async with database.transaction() as writer:
            await writer.execute(
                insert(run_inputs).values(
                    run_id=parent.run_id, cel_projection=projection, params=params
                )
            )
    return RunInputBinding(
        parent.namespace_id, parent.graph_id, parent.version_id, parent.run_id
    )


@pytest.mark.asyncio
async def test_run_input_read_refuses_unavailable_snapshot(
    database: Database,
) -> None:
    binding = await seed(database)
    settings = database.settings

    with TemporaryDirectory(prefix="kora-read-", dir="/tmp") as directory:
        if database.dialect is Dialect.POSTGRESQL:
            url = settings.url.set(host=directory)
        else:
            url = settings.url.set(database=directory)
        probe = Database(replace(settings, url=url))
        ledger = ConnectionLedger(probe)
        try:
            refused = await read_run_input(
                probe, binding, RunInputField.PROJECTION, LIMITS
            )
        finally:
            await probe.close()

    assert refused is None
    assert ledger.checkouts == ledger.outstanding == probe.units_in_flight() == 0


@pytest.mark.asyncio
async def test_run_input_read_binds_both_exact_values_after_snapshot_release(
    database: Database,
) -> None:
    binding = await seed(database)
    limits = RunInputReadLimits(4096, len(PROJECTION), len(CANONICAL))
    ledger = ConnectionLedger(database)

    cut = await read_run_input(database, binding, RunInputField.PROJECTION, limits)
    original = await read_run_input(database, binding, RunInputField.PARAMS, limits)

    assert cut is not None
    assert original is not None
    assert cut.binding == original.binding == binding
    assert cut.field is RunInputField.PROJECTION
    assert original.field is RunInputField.PARAMS
    assert cut.value == {"cut": 1}
    assert original.value["\0key"] == "\0value"
    assert original.value["unicode"] == ["é", "e\u0301", "😀", '{["]}']
    numbers = original.value["numbers"]
    assert type(numbers) is list
    assert tuple(map(type, numbers)) == (
        int,
        int,
        int,
        float,
        bool,
        float,
        float,
        float,
    )
    signed_zero = numbers[5]
    assert type(signed_zero) is float
    assert math.copysign(1.0, signed_zero) == -1.0
    assert ledger.checkouts == 2
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_run_input_read_does_not_query_side_for_wrong_parent(
    database: Database,
) -> None:
    binding = await seed(database)
    wrong = replace(binding, version_id=binding.graph_id)
    statements: list[str] = []

    def capture(
        connection: Connection, cursor: DBAPICursor, statement: str, *rest: object
    ) -> None:
        if is_side_query(statement):
            statements.append(statement)

    engine = database.reader_engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        refused = await read_run_input(
            database, wrong, RunInputField.PROJECTION, LIMITS
        )
        excessive = await read_run_input(
            database,
            binding,
            RunInputField.PROJECTION,
            replace(LIMITS, params=MAX_PARAMS_BYTES + 1),
        )
        before_qualified = tuple(statements)
        qualified = await read_run_input(
            database, binding, RunInputField.PROJECTION, LIMITS
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert refused is excessive is None
    assert before_qualified == ()
    assert qualified is not None
    assert len(statements) == 1
    assert "cel_projection" in statements[0]
    assert "params" not in statements[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", list(RunInputField))
async def test_run_input_read_refuses_missing_owned_side_row(
    database: Database, field: RunInputField
) -> None:
    binding = await seed(database, side_row=False)

    result = await read_run_input(database, binding, field, LIMITS)

    assert result is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_run_input_read_uses_the_exact_main_partial_side_index(
    tmp_path: Path, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    statements: list[tuple[str, tuple[object, ...]]] = []

    def capture(
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        arguments: tuple[object, ...],
        *rest: object,
    ) -> None:
        if is_side_query(statement):
            statements.append((statement, arguments))

    try:
        binding = await seed(database)
        async with database.transaction() as writer:
            for key in ("0" * (4 * 1024 * 1024), b"0" * (4 * 1024 * 1024)):
                neighbor = await Seeder(writer).run(
                    binding.namespace_id, binding.graph_id, binding.version_id
                )
                await writer.exec_driver_sql(
                    "UPDATE main.run SET id = ? WHERE id = ?", (key, neighbor)
                )
                await writer.exec_driver_sql(
                    "INSERT INTO main.run_inputs (run_id, cel_projection, params) "
                    "VALUES (?, ?, ?)",
                    (key, b"{}", b"{}"),
                )
        async with database.reader_engine.connect() as setup:
            await setup.exec_driver_sql("PRAGMA query_only = 0")
            await setup.exec_driver_sql("CREATE TABLE temp.run_inputs (wrong)")
            await setup.exec_driver_sql(
                "CREATE INDEX temp.ix_run_inputs_locator ON run_inputs(wrong)"
            )
            await setup.exec_driver_sql("PRAGMA query_only = 1")
            await setup.commit()

        engine = database.reader_engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            cut = await read_run_input(
                database, binding, RunInputField.PROJECTION, LIMITS
            )
            raw = await read_run_input(database, binding, RunInputField.PARAMS, LIMITS)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        async with database.read_snapshot() as reader:
            plans = [
                [
                    tuple(row)[3]
                    for row in await reader.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + statement, arguments
                    )
                ]
                for statement, arguments in statements
            ]
            instructions = [
                await sqlite_instructions(reader, statement, arguments)
                for statement, arguments in statements
            ]
        captured = tuple(statements)
        async with database.transaction() as writer:
            await writer.exec_driver_sql("DROP INDEX main.ix_run_inputs_locator")
        event.listen(engine, "before_cursor_execute", capture)
        try:
            refused = await read_run_input(
                database, binding, RunInputField.PROJECTION, LIMITS
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert cut is not None
        assert raw is not None
        assert cut.binding == raw.binding == binding
        assert len(statements) == 3
        assert tuple(statements[:2]) == captured
        assert statements[2] == statements[0]
        assert refused is None
        assert "params" not in statements[0][0]
        assert "params" in statements[1][0]
        for program, field in zip(instructions, RunInputField, strict=True):
            assert_sqlite_side_guards_value_reads(program, field)
        for plan in plans:
            assert len(plan) == 1
            detail: object = plan[0]
            assert isinstance(detail, str)
            assert detail.startswith("SEARCH ")
            assert detail.endswith("USING INDEX ix_run_inputs_locator (run_id=?)")
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_read_applies_each_limit_in_sql(database: Database) -> None:
    binding = await seed(database)
    tight_cut = replace(LIMITS, projection=len(PROJECTION) - 1)
    tight_raw = replace(LIMITS, params=len(CANONICAL) - 1)
    cases = (
        (RunInputField.PROJECTION, tight_cut),
        (RunInputField.PARAMS, tight_cut),
        (RunInputField.PARAMS, tight_raw),
        (RunInputField.PROJECTION, tight_raw),
    )

    async with database.read_snapshot() as connection:
        native = [
            await connection.scalar(
                run_input_value_statement(
                    database.dialect, binding.run_id, field, limit
                )
            )
            for field, limit in cases
        ]
    outcomes = [
        await read_run_input(database, binding, field, limit) for field, limit in cases
    ]

    assert native == [None, None, None, PROJECTION]
    assert outcomes[:3] == [None, None, None]
    assert outcomes[3] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_field", list(RunInputField))
async def test_run_input_read_decodes_only_the_selected_value(
    database: Database, invalid_field: RunInputField
) -> None:
    invalid = b'{"duplicate":1,"duplicate":2}'
    binding = await seed(
        database,
        projection=invalid if invalid_field is RunInputField.PROJECTION else PROJECTION,
        params=invalid if invalid_field is RunInputField.PARAMS else CANONICAL,
    )
    other = (
        RunInputField.PARAMS
        if invalid_field is RunInputField.PROJECTION
        else RunInputField.PROJECTION
    )

    rejected = await read_run_input(database, binding, invalid_field, LIMITS)
    accepted = await read_run_input(database, binding, other, LIMITS)

    assert rejected is None
    assert accepted is not None
    assert accepted.field is other


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text_value", [PROJECTION.decode(), "x" * 65_536], ids=["within", "over"]
)
async def test_run_input_read_refuses_sqlite_text_before_raw_read(
    tmp_path: Path, text_value: str
) -> None:
    settings = sqlite_settings(tmp_path / "bad-side.sqlite")
    await upgrade_database(settings)
    database = Database(settings)
    try:
        binding = await seed(database)
        async with database.transaction() as writer:
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
            try:
                await writer.exec_driver_sql(
                    "UPDATE run_inputs SET cel_projection=? WHERE run_id=?",
                    (text_value, binding.run_id),
                )
            finally:
                await writer.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")

        async with database.read_snapshot() as reader:
            native = [
                await reader.scalar(
                    run_input_value_statement(
                        database.dialect, binding.run_id, field, LIMITS
                    )
                )
                for field in RunInputField
            ]
        checked = [
            await read_run_input(database, binding, field, LIMITS)
            for field in RunInputField
        ]
    finally:
        await database.close()

    assert native == checked == [None, None]


@pytest.mark.asyncio
async def test_run_input_read_keeps_parent_and_side_in_one_snapshot(
    database: Database,
) -> None:
    binding = await seed(database)
    parent_read = asyncio.Event()
    release = asyncio.Event()

    def after_parent(
        connection: Connection, cursor: DBAPICursor, statement: str, *rest: object
    ) -> None:
        if is_parent_query(statement):
            parent_read.set()
            await_(release.wait())

    engine = database.reader_engine.sync_engine
    event.listen(engine, "after_cursor_execute", after_parent)
    pending = asyncio.create_task(
        read_run_input(database, binding, RunInputField.PROJECTION, LIMITS)
    )
    try:
        await asyncio.wait_for(parent_read.wait(), CONDITION_DEADLINE_SECONDS)
        async with database.transaction() as writer:
            await writer.execute(
                update(run).where(run.c.id == binding.run_id).values(params=PRIVATE)
            )
            await writer.execute(
                update(run_inputs)
                .where(run_inputs.c.run_id == binding.run_id)
                .values(cel_projection=b'{"new":true}')
            )
        release.set()
        stable = await asyncio.wait_for(pending, CONDITION_DEADLINE_SECONDS)
        later = await read_run_input(
            database, binding, RunInputField.PROJECTION, LIMITS
        )
    finally:
        event.remove(engine, "after_cursor_execute", after_parent)
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    assert stable is not None
    assert stable.value == {"cut": 1}
    assert later is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_run_input_read_cancellation_releases_side_snapshot(
    database: Database,
) -> None:
    binding = await seed(database)
    dispatched = asyncio.Event()
    held = asyncio.Event()
    ledger = ConnectionLedger(database)

    def hold(
        connection: Connection, cursor: DBAPICursor, statement: str, *rest: object
    ) -> None:
        if is_side_query(statement):
            dispatched.set()
            await_(held.wait())

    engine = database.reader_engine.sync_engine
    event.listen(engine, "after_cursor_execute", hold)
    pending = asyncio.create_task(
        read_run_input(database, binding, RunInputField.PROJECTION, LIMITS)
    )
    try:
        await asyncio.wait_for(dispatched.wait(), CONDITION_DEADLINE_SECONDS)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, CONDITION_DEADLINE_SECONDS)
    finally:
        event.remove(engine, "after_cursor_execute", hold)
        held.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    reread = await read_run_input(database, binding, RunInputField.PROJECTION, LIMITS)

    assert reread is not None
    assert ledger.checkouts == 2
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("compressible", [True, False])
@pytest.mark.parametrize("oversized", [None, *RunInputField])
async def test_run_input_read_admits_maximum_values_before_driver_transfer(
    database: Database, compressible: bool, oversized: RunInputField | None
) -> None:
    value_width = MAX_PARAMS_BYTES - len(b'{"value":""}')
    value = (
        b"a" * value_width
        if compressible
        else base64.b64encode(hashlib.shake_256(b"external input").digest(value_width))[
            :value_width
        ]
    )
    exact = b'{"value":"' + value + b'"}'
    wide = exact[:-2] + b'a"}'
    projection = wide if oversized is RunInputField.PROJECTION else exact
    params = wide if oversized is RunInputField.PARAMS else exact
    binding = await seed(database, projection=projection, params=params)
    limits = replace(LIMITS, projection=MAX_PARAMS_BYTES)
    ledger = ConnectionLedger(database)

    async with database.read_snapshot() as connection:
        native = [
            await connection.scalar(
                run_input_value_statement(
                    database.dialect, binding.run_id, field, limits
                )
            )
            for field in RunInputField
        ]
        if database.dialect is Dialect.POSTGRESQL:
            metadata = [
                tuple(
                    (
                        await connection.execute(
                            select(
                                func.pg_catalog.pg_column_size(column),
                                func.pg_catalog.pg_column_compression(column),
                            ).where(run_inputs.c.run_id == binding.run_id)
                        )
                    ).one()
                )
                for column in (run_inputs.c.cel_projection, run_inputs.c.params)
            ]
        else:
            metadata = []
    outcomes = [
        await read_run_input(database, binding, field, limits)
        for field in RunInputField
    ]

    assert len(exact) == MAX_PARAMS_BYTES
    assert len(wide) == MAX_PARAMS_BYTES + 1
    for field, transferred, checked in zip(
        RunInputField, native, outcomes, strict=True
    ):
        refused = oversized is RunInputField.PROJECTION or field is oversized
        if refused:
            assert transferred is checked is None
        else:
            assert transferred == exact
            assert checked is not None
            assert checked.binding == binding
            assert checked.field is field
            assert checked.value == {"value": value.decode()}
    for physical, compression in metadata:
        assert type(physical) is int
        if compressible:
            assert compression is not None
            assert physical < MAX_PARAMS_BYTES
        else:
            assert compression is None
            assert physical >= MAX_PARAMS_BYTES
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
