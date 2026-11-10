import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from traceback import format_exception

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.maintenance import (
    PUBLICATION_TRIGGER_CHECK,
    SQLitePublicationTriggerObservation,
    observe_sqlite_publication_triggers,
)
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_sqlite_writer_entry import writer_driver

TARGETS = (
    "GrApH",
    "GrApH_VeRsIoN",
    "GrApH_VeRsIoN_ReAd_AdMiSsIoN",
    "GrApH_VeRsIoN_ReSoUrCe_OwNeR",
    "GrApH_DePlOyMeNt_PrEpArAtIoN",
    "IdEmPoTeNcY_ReCoRd",
    "EvEnT",
)
MAIN_TRIGGER = "CREATE TRIGGER main.publication_probe"
TEMP_TRIGGER = "CREATE TEMP TRIGGER publication_probe"
CHECK_MAIN_TRIGGER = text(
    "SELECT tbl_name FROM main.sqlite_schema WHERE name = 'publication_probe'"
)
CHECK_TEMP_TRIGGER = text(
    "SELECT tbl_name FROM temp.sqlite_schema WHERE name = 'publication_probe'"
)
CREATE_CANARY = text("CREATE TABLE publication_trigger_canary (value INTEGER)")
INSERT_CANARY = text("INSERT INTO publication_trigger_canary VALUES (7)")
READ_CANARY = text("SELECT value FROM publication_trigger_canary")


class RollbackRefusal(RuntimeError):
    pass


async def assert_refused_before_sql(connection: AsyncConnection) -> None:
    with executed_queries(connection) as queries:
        result = await observe_sqlite_publication_triggers(connection)

    assert result is SQLitePublicationTriggerObservation.REFUSED
    assert queries == []


@pytest.mark.asyncio
async def test_publication_trigger_observation_requires_live_managed_sqlite_writer(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    sqlite = database.dialect is Dialect.SQLITE

    async with database.read_transaction() as reader:
        await assert_refused_before_sql(reader)
    async with database.transaction() as writer:
        async with writer.begin_nested():
            await assert_refused_before_sql(writer)
        await asyncio.create_task(assert_refused_before_sql(writer))
        with executed_queries(writer) as queries:
            current = await observe_sqlite_publication_triggers(writer)
        if sqlite:
            await writer.exec_driver_sql("ROLLBACK")
            assert writer.in_transaction()
            await assert_refused_before_sql(writer)
            driver = await writer_driver(writer)
            await driver.close()
            await assert_refused_before_sql(writer)
            await writer.invalidate()
            await writer.rollback()
    await assert_refused_before_sql(writer)

    assert current is (
        SQLitePublicationTriggerObservation.VERIFIED
        if sqlite
        else SQLitePublicationTriggerObservation.REFUSED
    )
    assert queries == ([(PUBLICATION_TRIGGER_CHECK.text, ())] if sqlite else [])
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_trigger_observation_checks_all_main_and_temp_targets(
    database: Database,
) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await assert_refused_before_sql(writer)
        return
    ledger = ConnectionLedger(database)
    observations: list[tuple[object, SQLitePublicationTriggerObservation]] = []

    async with database.transaction() as writer:
        await writer.exec_driver_sql(
            "CREATE TRIGGER main.unrelated AFTER INSERT ON main.namespace "
            "BEGIN SELECT 1; END"
        )
        unrelated = await observe_sqlite_publication_triggers(writer)
        for create, check, drop in (
            (MAIN_TRIGGER, CHECK_MAIN_TRIGGER, "DROP TRIGGER main.publication_probe"),
            (TEMP_TRIGGER, CHECK_TEMP_TRIGGER, "DROP TRIGGER temp.publication_probe"),
        ):
            for target in TARGETS:
                await writer.exec_driver_sql(
                    f"{create} AFTER INSERT ON main.{target} BEGIN SELECT 1; END"
                )
                native_target: object = await writer.scalar(check)
                result = await observe_sqlite_publication_triggers(writer)
                observations.append((native_target, result))
                await writer.exec_driver_sql(drop)
        recovered = await observe_sqlite_publication_triggers(writer)

    assert unrelated is recovered is SQLitePublicationTriggerObservation.VERIFIED
    assert observations == [
        (target, SQLitePublicationTriggerObservation.REFUSED) for target in TARGETS * 2
    ]
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


def install_native_trigger(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as native:
        with closing(
            native.execute(
                "CREATE TRIGGER main.late_publication_probe "
                "AFTER INSERT ON main.event BEGIN SELECT 1; END"
            )
        ):
            pass


@pytest.mark.asyncio
async def test_publication_trigger_observation_keeps_the_original_sqlite_writer(
    database: Database,
) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await assert_refused_before_sql(writer)
        return
    ledger = ConnectionLedger(database)
    path = database.settings.sqlite_path

    async with database.transaction() as writer:
        original = await observe_sqlite_publication_triggers(writer)
        with pytest.raises(sqlite3.OperationalError) as contended:
            await asyncio.to_thread(install_native_trigger, path)
        still_clear = await observe_sqlite_publication_triggers(writer)
    await asyncio.to_thread(install_native_trigger, path)
    async with database.transaction() as writer:
        later = await observe_sqlite_publication_triggers(writer)

    assert original is still_clear is SQLitePublicationTriggerObservation.VERIFIED
    assert contended.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert later is SQLitePublicationTriggerObservation.REFUSED
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_publication_trigger_native_failure_releases_writer(
    database: Database, cancel: bool
) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await assert_refused_before_sql(writer)
        return
    ledger = ConnectionLedger(database)
    denials: list[int] = []
    cancellations: list[int] = []

    def deny_catalog(
        action: int,
        name: str | None,
        column: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and name in (
            "sqlite_master",
            "sqlite_temp_master",
        ):
            denials.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def cancel_at_dispatch(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == PUBLICATION_TRIGGER_CHECK.text:
            task = asyncio.current_task()
            assert task is not None
            cancellations.append(ledger.outstanding)
            asyncio.get_running_loop().call_soon(task.cancel)

    async def observe() -> None:
        async with database.transaction() as writer:
            await writer.execute(INSERT_CANARY)
            if cancel:
                event.listen(
                    writer.sync_connection, "before_cursor_execute", cancel_at_dispatch
                )
                try:
                    await observe_sqlite_publication_triggers(writer)
                finally:
                    event.remove(
                        writer.sync_connection,
                        "before_cursor_execute",
                        cancel_at_dispatch,
                    )
            else:
                driver = await writer_driver(writer)
                await driver.set_authorizer(deny_catalog)
                try:
                    result = await observe_sqlite_publication_triggers(writer)
                finally:
                    await driver.set_authorizer(None)
                assert result is SQLitePublicationTriggerObservation.REFUSED
                raise RollbackRefusal()

    async with database.transaction() as writer:
        await writer.execute(CREATE_CANARY)
    expected_error = asyncio.CancelledError if cancel else RollbackRefusal

    with pytest.raises(expected_error) as refused:
        await asyncio.wait_for(observe(), timeout=CONDITION_DEADLINE_SECONDS)
    released = (
        ledger.outstanding == database.units_in_flight() == 0
        and database.writer_is_free()
    )
    await asyncio.to_thread(install_native_trigger, database.settings.sqlite_path)
    async with database.transaction() as writer:
        canary = (await writer.execute(READ_CANARY)).all()
        native_write_visible = await observe_sqlite_publication_triggers(writer)
        await writer.exec_driver_sql("DROP TRIGGER main.late_publication_probe")
        recovered = await observe_sqlite_publication_triggers(writer)

    assert denials == ([] if cancel else [sqlite3.SQLITE_READ])
    assert cancellations == ([1] if cancel else [])
    assert released
    assert canary == []
    assert native_write_visible is SQLitePublicationTriggerObservation.REFUSED
    assert recovered is SQLitePublicationTriggerObservation.VERIFIED
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert PUBLICATION_TRIGGER_CHECK.text not in "".join(
        format_exception(refused.value)
    )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
