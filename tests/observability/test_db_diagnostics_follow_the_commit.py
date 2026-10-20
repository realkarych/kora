import asyncio
from typing import cast

import pytest
from sqlalchemy import Connection, event

from kora.db import DB_COMPONENT
from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import upgrade_database
from kora.observability import Level, LoggingSettings
from tests.db.seed import Seeder
from tests.observability.conftest import Configure, Pipe

DB_EVENTS: frozenset[str] = frozenset(
    {
        "db.migration.started",
        "db.migration.finished",
        "db.migration.failed",
        "db.transaction.failed",
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

    assert report.steps == 1
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


def refusing_commits(settings: DatabaseSettings) -> Database:
    database = Database(settings)
    event.listen(database.writer_engine.sync_engine, "commit", refuse_commit)
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
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("outlived")
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

    holding = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 10)
    await database.close(deadline_seconds=0.2)
    release.set()
    await holding
    diagnostics.shutdown()
    finished_output(pipe)

    timed_out = [
        record
        for record in pipe.reader.records()
        if record["event"] == "db.close.timed_out"
    ]
    assert len(timed_out) == 1
    assert timed_out[0]["level"] == "WARNING"
    assert timed_out[0]["units_in_flight"] == 1
