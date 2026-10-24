import asyncio
from typing import cast

import pytest
from sqlalchemy import Connection, event, inspect, text

from kora.db import DB_COMPONENT
from kora.db.connection import (
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    Dialect,
    Lifecycle,
)
from kora.db.migrations import upgrade_database
from kora.observability import Level, LoggingSettings
from tests.db.closing import ShutdownDuringClose, narrow_database, until
from tests.db.seed import Seeder, seed_predecessor
from tests.db.test_pricing_migration import retained_index_progress, wait_for_index
from tests.observability.conftest import Configure, Pipe

DB_EVENTS: frozenset[str] = frozenset(
    {
        "db.migration.started",
        "db.migration.finished",
        "db.migration.failed",
        "db.transaction.failed",
        "db.close.timed_out",
    }
)


class CommitRefused(RuntimeError):
    pass


class BodyFault(ValueError):
    pass


def refuse_commit(connection: Connection) -> None:
    raise CommitRefused()


def db_events(pipe: Pipe) -> list[str]:
    return [
        str(record["event"])
        for record in pipe.reader.records()
        if record["event"] in DB_EVENTS
    ]


def finished_output(pipe: Pipe) -> str:
    pipe.close_write_end()
    pipe.reader.wait_closed()
    return "\n".join(pipe.reader.lines())


@pytest.mark.asyncio
async def test_a_refused_commit_records_failure_and_never_finished(
    configure: Configure, pipe: Pipe, empty_database_settings: DatabaseSettings
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()
    probe = Database(empty_database_settings)
    event.listen(probe.writer_engine.sync_engine, "commit", refuse_commit)

    with pytest.raises(CommitRefused):
        async with probe.transaction() as connection:
            await connection.run_sync(lambda sync: None)
    await probe.close()
    report = await upgrade_database(empty_database_settings)
    diagnostics.shutdown()
    finished_output(pipe)

    assert report.steps == 2
    assert db_events(pipe) == [
        "db.transaction.failed",
        "db.migration.started",
        "db.migration.finished",
    ]


@pytest.mark.asyncio
async def test_a_refused_migration_commit_leaves_no_finished_record(
    configure: Configure, pipe: Pipe, empty_database_settings: DatabaseSettings
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()

    with pytest.raises(CommitRefused):
        await upgrade_refusing_commit(empty_database_settings)
    diagnostics.shutdown()
    finished_output(pipe)

    assert db_events(pipe) == [
        "db.migration.started",
        "db.transaction.failed",
        "db.migration.failed",
    ]


def refuse_migration_commit(connection: Connection) -> None:
    if inspect(connection).has_table("alembic_version"):
        raise CommitRefused()


def refusing_commits(settings: DatabaseSettings) -> Database:
    database = Database(settings)
    event.listen(database.writer_engine.sync_engine, "commit", refuse_migration_commit)
    return database


async def upgrade_refusing_commit(settings: DatabaseSettings) -> None:
    await upgrade_database(settings, refusing_commits)


async def faulting_unit(database: Database) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace("faulty")
        raise BodyFault()


@pytest.mark.asyncio
async def test_body_faults_are_recorded_once_and_cancellations_never(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()
    entered = asyncio.Event()

    with pytest.raises(BodyFault):
        await faulting_unit(database)

    async def cancelled_unit() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("cancelled")
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(cancelled_unit())
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    diagnostics.shutdown()
    finished_output(pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "db.transaction.failed"
    ]
    assert len(records) == 1
    chain = cast(list[dict[str, object]], records[0]["exception"])
    assert chain[0]["type"] == "BodyFault"


@pytest.mark.asyncio
async def test_close_after_the_deadline_is_recorded(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()
    closing = narrow_database(database, pool_size=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with closing.transaction() as connection:
            await Seeder(connection).namespace("outlived")
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

    async def queued() -> None:
        async with closing.transaction() as connection:
            await Seeder(connection).namespace("queued")

    holding = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 10)
    waiting = asyncio.create_task(queued())
    await until(lambda: closing.units_in_flight() == 2)

    await closing.close(deadline_seconds=0.2)
    release.set()
    results = await asyncio.gather(holding, waiting, return_exceptions=True)
    diagnostics.shutdown()
    finished_output(pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] in {"db.close.timed_out", "db.transaction.failed"}
    ]
    assert [type(result) for result in results] == [DatabaseClosedError] * 2
    assert [record["event"] for record in records] == [
        "db.close.timed_out",
        "db.transaction.failed",
    ]
    assert records[0]["level"] == "WARNING"
    assert records[0]["units_in_flight"] == 2
    assert records[1]["level"] == "DEBUG"
    chain = cast(list[dict[str, object]], records[1]["exception"])
    assert chain[0]["type"] == "DatabaseClosedError"


@pytest.mark.asyncio
async def test_an_idle_close_records_no_timeout_even_with_a_zero_deadline(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()
    closing = narrow_database(database)
    await write_one(closing)

    await closing.close(deadline_seconds=0)
    diagnostics.shutdown()
    finished_output(pipe)

    assert db_events(pipe) == []


@pytest.mark.asyncio
async def test_a_close_past_its_deadline_keeps_each_unit_outcome_and_records_once(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()
    closing = narrow_database(database)
    writer_entered = asyncio.Event()
    reader_entered = asyncio.Event()
    release = asyncio.Event()

    async def faulting_holder() -> None:
        async with closing.transaction() as connection:
            await Seeder(connection).namespace("outlived")
            writer_entered.set()
            await asyncio.wait_for(release.wait(), 10)
            raise BodyFault()

    async def reader() -> None:
        async with closing.read_transaction() as connection:
            await connection.execute(text("SELECT 1"))
            reader_entered.set()
            await asyncio.wait_for(release.wait(), 10)

    holding = asyncio.create_task(faulting_holder())
    await asyncio.wait_for(writer_entered.wait(), 10)
    reading = asyncio.create_task(reader())
    await asyncio.wait_for(reader_entered.wait(), 10)

    await asyncio.gather(
        closing.close(deadline_seconds=0.2), closing.close(deadline_seconds=0)
    )
    reading.cancel()
    release.set()
    results = await asyncio.gather(holding, reading, return_exceptions=True)
    diagnostics.shutdown()
    finished_output(pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] in {"db.close.timed_out", "db.transaction.failed"}
    ]
    assert [type(result) for result in results] == [
        BodyFault,
        asyncio.CancelledError,
    ]
    assert [record["event"] for record in records] == [
        "db.close.timed_out",
        "db.transaction.failed",
    ]
    assert records[0]["units_in_flight"] == 2
    chain = cast(list[dict[str, object]], records[1]["exception"])
    assert chain[0]["type"] == "BodyFault"


async def write_one(database: Database) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace("before-close")


@pytest.mark.parametrize(
    "close_started", [True, False], ids=["close-waiting", "close-not-started"]
)
def test_a_close_cancelled_by_loop_shutdown_records_nothing(
    configure: Configure,
    pipe: Pipe,
    database_settings: DatabaseSettings,
    close_started: bool,
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()
    shutdown = ShutdownDuringClose(database_settings)

    asyncio.run(shutdown.leave_a_close_pending(close_started))
    diagnostics.shutdown()
    finished_output(pipe)

    assert db_events(pipe) == []
    assert shutdown.lifecycle_after_shutdown() is Lifecycle.CLOSED


@pytest.mark.asyncio
async def test_cancelled_pricing_upgrade_keeps_diagnostics_content_free(
    configure: Configure, pipe: Pipe, empty_database_settings: DatabaseSettings
) -> None:
    if empty_database_settings.dialect is Dialect.POSTGRESQL:
        await retained_index_progress(empty_database_settings)
    else:
        initial = Database(empty_database_settings)
        try:
            async with initial.transaction() as connection:
                await connection.run_sync(seed_predecessor)
        finally:
            await initial.close()
    diagnostics = configure(
        LoggingSettings(
            level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
        ),
        pipe.write_end,
    )
    pipe.reader.start()
    database = Database(empty_database_settings)
    opened: list[Database] = []

    def open_upgrade(settings: DatabaseSettings) -> Database:
        migration = Database(settings)
        opened.append(migration)
        return migration

    try:
        async with database.transaction() as holder:
            await holder.exec_driver_sql(
                "UPDATE price_table SET model = model WHERE id = 'old-price'"
            )
            task = asyncio.create_task(
                upgrade_database(empty_database_settings, open_upgrade)
            )
            if database.dialect is Dialect.POSTGRESQL:
                await wait_for_index(database)
            else:
                await until(lambda: bool(opened) and opened[0].units_in_flight() == 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        diagnostics.shutdown()
        finished_output(pipe)

        assert set(db_events(pipe)) <= {"db.migration.started"}
        assert opened[0].units_in_flight() == 0
        assert opened[0].lifecycle is Lifecycle.CLOSED
    finally:
        await database.close()
