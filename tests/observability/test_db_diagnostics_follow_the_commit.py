import asyncio
from traceback import format_exception
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
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.migrations import upgrade_database
from kora.engine.locks import (
    GraphPreparationLockFailed,
    LockError,
    PreparationCapacityReadFailed,
    StorageAddressLockFailed,
    StorageAddressStageReadFailed,
    TransitionLocks,
)
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger, narrow_database, until
from tests.db.seed import Seeder, seed_predecessor
from tests.db.test_graph_preparation_reads import seed_preparation
from tests.db.test_pricing_migration import retained_index_progress, wait_for_index
from tests.engine.test_preparation_capacity_lock import (
    CAPACITY,
    insert_ledger,
    read_capacity,
)
from tests.engine.test_storage_address_lock import DIGEST, STORE, seed_address
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
)

DB_DEBUG: LoggingSettings = LoggingSettings(
    level=Level.DEBUG, component_levels={DB_COMPONENT: Level.DEBUG}
)
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


@pytest.mark.asyncio
async def test_a_refused_commit_records_failure_and_never_finished(
    configure: Configure, pipe: Pipe, empty_database_settings: DatabaseSettings
) -> None:
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
    probe = Database(empty_database_settings)
    event.listen(probe.writer_engine.sync_engine, "commit", refuse_commit)

    with pytest.raises(CommitRefused):
        async with probe.transaction() as connection:
            await connection.run_sync(lambda sync: None)
    await probe.close()
    report = await upgrade_database(empty_database_settings)
    finished_output(diagnostics, pipe)

    assert report.steps == 28
    assert db_events(pipe) == [
        "db.transaction.failed",
        "db.migration.started",
        "db.migration.finished",
    ]


@pytest.mark.asyncio
async def test_a_refused_migration_commit_leaves_no_finished_record(
    configure: Configure, pipe: Pipe, empty_database_settings: DatabaseSettings
) -> None:
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)

    with pytest.raises(CommitRefused):
        await upgrade_refusing_commit(empty_database_settings)
    finished_output(diagnostics, pipe)

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
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
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
    finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "db.transaction.failed"
    ]
    assert len(records) == 1
    chain = cast(list[dict[str, object]], records[0]["exception"])
    assert chain[0]["type"] == "BodyFault"


async def lock_address_in_transaction(
    database: Database, store: str, digest: str
) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes(())
        await locks.acquire_existing_storage_address(store, digest)


@pytest.mark.asyncio
async def test_address_lock_statement_failure_keeps_private_keys_out_of_diagnostics(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    store = "Private-Physical-Store"
    digest = "sha256:" + "a" * 64
    ambient = ValueError(store)
    async with database.transaction() as connection:
        await connection.exec_driver_sql(
            "ALTER TABLE storage_address_fence RENAME COLUMN payload_digest "
            "TO unavailable_payload_digest"
        )
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)

    try:
        raise ambient
    except ValueError:
        with pytest.raises(
            StorageAddressLockFailed, match="fence lock failed"
        ) as refused:
            await lock_address_in_transaction(database, store, digest)
    finished_output(diagnostics, pipe)
    error = refused.value
    rendered = "".join(format_exception(error))
    records = pipe.reader.records()

    assert error.__cause__ is None
    assert error.__suppress_context__
    assert any(error.__context__ is context for context in (None, ambient))
    assert db_events(pipe) == ["db.transaction.failed"]
    for private in (store, digest, "unavailable_payload_digest", "UndefinedColumn"):
        assert private not in rendered
        assert private not in repr(records)


async def lock_preparation_in_transaction(
    database: Database, binding: GraphPreparationBinding
) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes(())
        await locks.acquire_existing_storage_address(STORE, DIGEST)
        await locks.acquire_open_graph_preparation(connection, STORE, DIGEST, binding)


@pytest.mark.asyncio
async def test_preparation_guard_sql_failure_keeps_bindings_out_of_diagnostics(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=True)
        await connection.exec_driver_sql(
            "ALTER TABLE graph_deployment_preparation RENAME COLUMN deployment_digest "
            "TO unavailable_deployment_digest"
        )
    assert binding.initial_graph_id is not None
    ambient = ValueError(binding.preparation_id)
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)

    try:
        raise ambient
    except ValueError:
        with pytest.raises(
            GraphPreparationLockFailed, match="preparation lock failed"
        ) as refused:
            await lock_preparation_in_transaction(database, binding)
    finished_output(diagnostics, pipe)
    error = refused.value
    rendered = "".join(format_exception(error))
    records = pipe.reader.records()

    assert error.__cause__ is None
    assert error.__suppress_context__
    assert any(error.__context__ is context for context in (None, ambient))
    assert db_events(pipe) == ["db.transaction.failed"]
    for private in (
        binding.preparation_id,
        binding.namespace_id,
        binding.graph_name,
        binding.initial_graph_id,
        binding.deployment_digest,
        "unavailable_deployment_digest",
        "UndefinedColumn",
    ):
        assert private not in rendered
        assert private not in repr(records)


async def fail_stage_fence_read(
    database: Database, binding: GraphPreparationBinding
) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes(())
        await locks.acquire_existing_storage_address(STORE, DIGEST)
        await locks.acquire_open_graph_preparation(connection, STORE, DIGEST, binding)
        await connection.exec_driver_sql(
            "ALTER TABLE storage_address_fence RENAME COLUMN last_deletion_generation "
            "TO unavailable_last_deletion_generation"
        )
        try:
            await locks.read_stage_fence_incarnation(connection, STORE, DIGEST, binding)
        except StorageAddressStageReadFailed:
            with pytest.raises(LockError, match="cannot be used"):
                locks.require_existing_storage_address(connection, STORE, DIGEST)
            raise


@pytest.mark.asyncio
async def test_stage_fence_read_failure_keeps_address_out_of_diagnostics(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
    ledger = ConnectionLedger(database)
    ambient = ValueError(DIGEST)
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)

    try:
        raise ambient
    except ValueError:
        with pytest.raises(
            StorageAddressStageReadFailed, match="staging check failed"
        ) as refused:
            await fail_stage_fence_read(database, binding)
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes(())
        await locks.acquire_existing_storage_address(STORE, DIGEST)
        await locks.acquire_open_graph_preparation(connection, STORE, DIGEST, binding)
        incarnation = await locks.read_stage_fence_incarnation(
            connection, STORE, DIGEST, binding
        )
    finished_output(diagnostics, pipe)
    error = refused.value
    rendered = "".join(format_exception(error))
    records = pipe.reader.records()

    assert error.__cause__ is None
    assert error.__suppress_context__
    assert any(error.__context__ is context for context in (None, ambient))
    assert db_events(pipe) == ["db.transaction.failed"]
    assert incarnation == 1
    assert ledger.outstanding == 0
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    for private in (
        STORE,
        DIGEST,
        binding.preparation_id,
        "unavailable_last_deletion_generation",
        "UndefinedColumn",
    ):
        assert private not in rendered
        assert private not in repr(records)


async def fail_capacity_read(database: Database) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes((CAPACITY,))
        await connection.exec_driver_sql(
            "ALTER TABLE graph_deployment_preparation_capacity "
            "RENAME COLUMN occupied TO unavailable_occupied"
        )
        try:
            await locks.read_preparation_capacity(connection)
        except PreparationCapacityReadFailed:
            with pytest.raises(LockError, match="cannot be used"):
                locks.require(None, (CAPACITY,))
            raise


@pytest.mark.asyncio
async def test_capacity_read_failure_keeps_private_count_out_of_diagnostics(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    occupied = 418377219
    await insert_ledger(database, occupied)
    ledger = ConnectionLedger(database)
    ambient = ValueError(str(occupied))
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)

    try:
        raise ambient
    except ValueError:
        with pytest.raises(
            PreparationCapacityReadFailed, match="capacity read failed"
        ) as refused:
            await fail_capacity_read(database)
    after = await read_capacity(database)
    finished_output(diagnostics, pipe)
    error = refused.value
    rendered = "".join(format_exception(error))
    records = pipe.reader.records()

    assert error.__cause__ is None
    assert error.__suppress_context__
    assert any(error.__context__ is context for context in (None, ambient))
    assert db_events(pipe) == ["db.transaction.failed"]
    assert after == occupied
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert ledger.outstanding == 0
    for private in (str(occupied), "unavailable_occupied", "UndefinedColumn"):
        assert private not in rendered
        assert private not in repr(records)


@pytest.mark.asyncio
async def test_close_after_the_deadline_is_recorded(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
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
    finished_output(diagnostics, pipe)

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
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
    closing = narrow_database(database)
    await write_one(closing)

    await closing.close(deadline_seconds=0)
    finished_output(diagnostics, pipe)

    assert db_events(pipe) == []


@pytest.mark.asyncio
async def test_a_close_past_its_deadline_keeps_each_unit_outcome_and_records_once(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
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
    finished_output(diagnostics, pipe)

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
    diagnostics = configured_with_reader(configure, pipe, DB_DEBUG)
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
        finished_output(diagnostics, pipe)

        assert set(db_events(pipe)) <= {"db.migration.started"}
        assert opened[0].units_in_flight() == 0
        assert opened[0].lifecycle is Lifecycle.CLOSED
    finally:
        await database.close()
