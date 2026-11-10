from pathlib import Path

import pytest

from kora.db.connection import Database, Dialect
from kora.db.maintenance import (
    PUBLICATION_FOREIGN_KEY_CHECK,
    SQLitePublicationForeignKeyObservation,
    SQLitePublicationTriggerObservation,
    observe_sqlite_publication_foreign_keys,
    observe_sqlite_publication_triggers,
)
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_sqlite_writer_entry import writer_driver
from tests.support.databases import copy_sqlite_template


@pytest.mark.asyncio
async def test_publication_foreign_key_observation_uses_the_current_sqlite_writer(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    sqlite = database.dialect is Dialect.SQLITE

    async with database.read_transaction() as reader:
        with executed_queries(reader) as reader_queries:
            read_result = await observe_sqlite_publication_foreign_keys(reader)
    async with database.transaction() as writer:
        async with writer.begin_nested():
            with executed_queries(writer) as nested_queries:
                nested = await observe_sqlite_publication_foreign_keys(writer)
        with executed_queries(writer) as writer_queries:
            written = await observe_sqlite_publication_foreign_keys(writer)

    assert read_result is nested is SQLitePublicationForeignKeyObservation.REFUSED
    assert reader_queries == nested_queries == []
    assert written is (
        SQLitePublicationForeignKeyObservation.VERIFIED
        if sqlite
        else SQLitePublicationForeignKeyObservation.REFUSED
    )
    assert writer_queries == (
        [(PUBLICATION_FOREIGN_KEY_CHECK.text, ())] if sqlite else []
    )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_foreign_key_observation_checks_native_actions_and_shadows(
    tmp_path: Path, sqlite_template: Path
) -> None:
    database = Database(
        copy_sqlite_template(sqlite_template, tmp_path / "foreign-keys.db")
    )
    ledger = ConnectionLedger(database)
    temporary_results: list[SQLitePublicationForeignKeyObservation] = []
    main_results: list[SQLitePublicationForeignKeyObservation] = []

    try:
        async with database.transaction() as writer:
            baseline = await observe_sqlite_publication_foreign_keys(writer)
            await writer.exec_driver_sql(
                "CREATE TABLE main.fk_probe_parent (id INTEGER PRIMARY KEY)"
            )
            await writer.exec_driver_sql(
                "CREATE TABLE main.fk_probe_unrelated (parent INTEGER "
                "REFERENCES fk_probe_parent(id) ON DELETE CASCADE)"
            )
            unrelated = await observe_sqlite_publication_foreign_keys(writer)
            for name in ("pragma_foreign_keys", "pragma_foreign_key_list"):
                await writer.exec_driver_sql(
                    f"CREATE TEMP TABLE {name} (unused INTEGER)"
                )
                temporary_results.append(
                    await observe_sqlite_publication_foreign_keys(writer)
                )
                await writer.exec_driver_sql(f"DROP TABLE temp.{name}")
                await writer.exec_driver_sql(
                    f"CREATE TABLE main.{name} (unused INTEGER)"
                )
                main_results.append(
                    await observe_sqlite_publication_foreign_keys(writer)
                )
                await writer.exec_driver_sql(f"DROP TABLE main.{name}")
            await writer.exec_driver_sql(
                "CREATE TABLE main.fk_probe_incoming (parent TEXT "
                "REFERENCES GrApH_VeRsIoN(id) ON UPDATE CASCADE)"
            )
            incoming = await observe_sqlite_publication_foreign_keys(writer)
            user_triggers = await observe_sqlite_publication_triggers(writer)
            await writer.exec_driver_sql("DROP TABLE main.fk_probe_incoming")
            await writer.exec_driver_sql(
                "ALTER TABLE main.event ADD COLUMN fk_probe_parent INTEGER "
                "REFERENCES fk_probe_parent(id) ON DELETE SET NULL"
            )
            outgoing = await observe_sqlite_publication_foreign_keys(writer)
            await writer.rollback()
        async with database.transaction() as writer:
            recovered = await observe_sqlite_publication_foreign_keys(writer)
    finally:
        await database.close()

    assert (
        baseline
        is unrelated
        is recovered
        is SQLitePublicationForeignKeyObservation.VERIFIED
    )
    assert incoming is outgoing is SQLitePublicationForeignKeyObservation.REFUSED
    assert user_triggers is SQLitePublicationTriggerObservation.VERIFIED
    assert temporary_results == [SQLitePublicationForeignKeyObservation.VERIFIED] * 2
    assert main_results == [SQLitePublicationForeignKeyObservation.REFUSED] * 2
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    assert ledger.opened == ledger.closed > 0


@pytest.mark.asyncio
async def test_publication_foreign_key_observation_requires_native_enforcement(
    tmp_path: Path, sqlite_template: Path
) -> None:
    database = Database(
        copy_sqlite_template(sqlite_template, tmp_path / "foreign-keys.db")
    )
    ledger = ConnectionLedger(database)
    observed: list[tuple[bool, SQLitePublicationForeignKeyObservation]] = []

    try:
        for pragma in ("PRAGMA foreign_keys=OFF", "PRAGMA foreign_keys=ON"):
            async with database.writer_engine.connect() as connection:
                native = await writer_driver(connection)
                async with native.execute(pragma):
                    pass

            async with database.transaction() as writer:
                same_driver = await writer_driver(writer) is native
                result = await observe_sqlite_publication_foreign_keys(writer)
            observed.append((same_driver, result))
    finally:
        await database.close()

    assert observed == [
        (True, SQLitePublicationForeignKeyObservation.REFUSED),
        (True, SQLitePublicationForeignKeyObservation.VERIFIED),
    ]
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    assert ledger.opened == ledger.closed > 0
