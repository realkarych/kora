import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from kora.db import DB_COMPONENT
from kora.db.connection import (
    Database,
    Dialect,
)
from kora.db.run_input_reads import RunInputField
from kora.engine.run_input_reads import read_run_input
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.test_run_input_parent import seed as seed_run_input_parent
from tests.db.test_sqlite_writer_entry import writer_driver
from tests.engine.test_run_input_reads import LIMITS as RUN_INPUT_LIMITS
from tests.engine.test_run_input_reads import seed as seed_run_input_read
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)

DB_DEBUG: LoggingSettings = LoggingSettings(
    level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
)


class BodyFault(ValueError):
    pass


@pytest.mark.asyncio
async def test_run_input_parent_refusal_keeps_native_failure_private(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    binding = await seed_run_input_parent(database)
    settings = LoggingSettings(
        level=Level.DEBUG,
        component_levels={DB_COMPONENT: Level.DEBUG},
        third_party={"sqlalchemy": Level.DEBUG, "aiosqlite": Level.DEBUG},
    )
    diagnostics = configured_with_reader(configure, pipe, settings)
    ledger = ConnectionLedger(database)
    denied: list[str] = []

    def deny_parent(
        action: int,
        name: str | None,
        column: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and name == "run" and schema == "main":
            denied.append(name)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    async with database.read_snapshot() as reader:
        accepted = await binding.verify(reader)
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE run RENAME COLUMN input_encoding_version "
                "TO unavailable_run_input_version"
            )
    async with database.read_snapshot() as reader:
        if database.dialect is Dialect.SQLITE:
            driver = await writer_driver(reader)
            await driver.set_authorizer(deny_parent)
            try:
                refused = await binding.verify(reader)
            finally:
                await driver.set_authorizer(None)
        else:
            refused = await binding.verify(reader)
        await reader.rollback()
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert (accepted, refused) == (True, False)
    assert denied == (["run"] if database.dialect is Dialect.SQLITE else [])
    assert ledger.outstanding == database.units_in_flight() == 0
    assert [
        record["verified"]
        for record in records
        if record["event"] == "db.run_input_parent.checked"
    ] == [True, False]
    for private in (
        binding.namespace_id,
        binding.graph_id,
        binding.version_id,
        binding.run_id,
        "unavailable_run_input_version",
        "UndefinedColumn",
        "prohibited",
    ):
        assert private.encode() not in pipe.reader.data


@pytest.mark.asyncio
async def test_run_input_read_refusal_keeps_payload_and_native_failure_private(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    private_value = "private-run-input-diagnostic-value"
    binding = await seed_run_input_read(
        database, params=b'{"private":"' + private_value.encode() + b'" }'
    )
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
    ledger = ConnectionLedger(database)

    accepted = await read_run_input(
        database, binding, RunInputField.PROJECTION, RUN_INPUT_LIMITS
    )
    malformed = await read_run_input(
        database, binding, RunInputField.PARAMS, RUN_INPUT_LIMITS
    )
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE run_inputs RENAME COLUMN params "
                "TO unavailable_run_input_params"
            )
    try:
        raise BodyFault(private_value)
    except BodyFault:
        if database.dialect is Dialect.SQLITE:
            async with denied_sqlite_side_rows(database) as denied:
                failed = await read_run_input(
                    database, binding, RunInputField.PARAMS, RUN_INPUT_LIMITS
                )
        else:
            denied = []
            failed = await read_run_input(
                database, binding, RunInputField.PARAMS, RUN_INPUT_LIMITS
            )
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert accepted is not None
    assert malformed is failed is None
    assert denied == (["run_inputs"] if database.dialect is Dialect.SQLITE else [])
    assert ledger.outstanding == database.units_in_flight() == 0
    assert [
        record["verified"]
        for record in records
        if record["event"] == "engine.run_input.checked"
    ] == [True, False, False]
    for private in (
        binding.namespace_id,
        binding.graph_id,
        binding.version_id,
        binding.run_id,
        private_value,
        "unavailable_run_input_params",
        "UndefinedColumn",
        "BodyFault",
        "prohibited",
    ):
        assert private not in repr(records)


@asynccontextmanager
async def denied_sqlite_side_rows(database: Database) -> AsyncGenerator[list[str]]:
    denied: list[str] = []

    def deny_side_row(
        action: int,
        name: str | None,
        column: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and name == "run_inputs" and schema == "main":
            denied.append(name)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    async with database.reader_engine.connect() as setup:
        original = await writer_driver(setup)
        await original.set_authorizer(deny_side_row)
    try:
        yield denied
    finally:
        async with database.reader_engine.connect() as teardown:
            driver = await writer_driver(teardown)
            assert driver is original
            await driver.set_authorizer(None)


@pytest.mark.asyncio
async def test_run_input_side_native_refusal_releases_snapshot(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    binding = await seed_run_input_read(database)
    settings = database.settings
    if database.dialect is Dialect.POSTGRESQL:
        settings = replace(
            settings, url=settings.url.update_query_dict({"command_timeout": "1.0"})
        )
    probe = Database(settings)
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
    ledger = ConnectionLedger(probe)

    try:
        if probe.dialect is Dialect.POSTGRESQL:
            async with probe.transaction() as writer:
                await writer.exec_driver_sql(
                    "LOCK TABLE run_inputs IN ACCESS EXCLUSIVE MODE"
                )
                refused = await read_run_input(
                    probe, binding, RunInputField.PROJECTION, RUN_INPUT_LIMITS
                )
        else:
            async with denied_sqlite_side_rows(probe) as denied:
                refused = await read_run_input(
                    probe, binding, RunInputField.PROJECTION, RUN_INPUT_LIMITS
                )
            assert denied == ["run_inputs"]
        recovered = await read_run_input(
            probe, binding, RunInputField.PROJECTION, RUN_INPUT_LIMITS
        )
    finally:
        await probe.close()
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert refused is None
    assert recovered is not None
    assert ledger.outstanding == probe.units_in_flight() == 0
    assert ledger.opened == ledger.closed > 0
    assert [
        record["verified"]
        for record in records
        if record["event"] == "db.run_input_parent.checked"
    ] == [True, True]
    assert [
        record["verified"]
        for record in records
        if record["event"] == "engine.run_input.checked"
    ] == [False, True]
    for private in (binding.run_id, "prohibited", "TimeoutError"):
        assert private not in repr(records)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_run_input_read_survives_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    binding = await seed_run_input_read(database)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    first = await read_run_input(
        database, binding, RunInputField.PARAMS, RUN_INPUT_LIMITS
    )
    second = await read_run_input(
        database, binding, RunInputField.PARAMS, RUN_INPUT_LIMITS
    )
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert first is not None
    assert first == second
    assert first.binding == binding
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
