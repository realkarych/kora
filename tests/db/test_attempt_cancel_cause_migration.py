import asyncio

import pytest
from sqlalchemy import insert, inspect, select, update
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseClosedError, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.tables import attempt
from tests.db.cancel_cause_support import (
    UpgradeStage,
    attempt_schema_objects,
    capture_cause_predecessor,
    migration_lock_count,
    seed_attempts,
    seed_cause_predecessor,
    wait_for_validation,
)
from tests.db.seed import predecessor_snapshot
from tests.db.test_db_upgrade_process import events, upgrade
from tests.db.test_migrations import differences_ignoring_expression_indexes

CAUSES: tuple[str | None, ...] = (
    None,
    "cancel",
    "clear",
    "supersede",
    "timeout",
    "stop",
)


@pytest.mark.asyncio
async def test_attempt_cancel_cause_upgrade_preserves_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await seed_cause_predecessor(database, rich=True)

        report = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
            objects = await connection.run_sync(attempt_schema_objects)
            columns = await connection.run_sync(
                lambda sync: inspect(sync).get_columns("attempt")
            )
            values = (
                (await connection.execute(select(attempt.c.cancel_cause)))
                .scalars()
                .all()
            )
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
        async with database.transaction() as connection:
            await connection.execute(
                insert(before.attempt_table).values(**before.old_insert)
            )
        async with database.read_transaction() as connection:
            omitted = (
                await connection.execute(
                    select(attempt.c.cancel_cause).where(
                        attempt.c.id == before.old_insert["id"]
                    )
                )
            ).scalar_one()
        rerun = await upgrade_database(empty_database_settings)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0003",
            "0005",
            2,
        )
        assert after == before.history
        assert objects == before.schema_objects
        assert values
        assert all(value is None for value in values)
        column = next(column for column in columns if column["name"] == "cancel_cause")
        assert column["nullable"]
        assert column.get("default") is None
        assert "cancel_cause" not in before.old_insert
        assert omitted is None
        assert differences == []
        assert rerun.steps == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_attempt_cancel_cause_domain_is_exact(database: Database) -> None:
    identifiers = await seed_attempts(database)

    async with database.transaction() as connection:
        for identifier, cause in zip(identifiers, CAUSES, strict=True):
            await connection.execute(
                update(attempt)
                .where(attempt.c.id == identifier)
                .values(cancel_cause=cause)
            )
    for invalid in ("", "CANCEL", "cancel ", "unknown", "TIMEOUT"):
        with pytest.raises(DBAPIError):
            async with database.transaction() as connection:
                await connection.execute(
                    update(attempt)
                    .where(attempt.c.id == identifiers[0])
                    .values(cancel_cause=invalid, desired_state="cancel")
                )
    async with database.read_transaction() as connection:
        values = (
            (
                await connection.execute(
                    select(attempt.c.cancel_cause).order_by(attempt.c.number)
                )
            )
            .scalars()
            .all()
        )
        desired = (
            (await connection.execute(select(attempt.c.desired_state))).scalars().all()
        )

    assert tuple(values) == CAUSES
    assert desired == ["run"] * len(CAUSES)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["prepared-cancel", "validation-cancel", "shutdown"])
async def test_attempt_cancel_cause_upgrade_recovers_compatible_partial_schema(
    empty_database_settings: DatabaseSettings, ending: str
) -> None:
    database = Database(empty_database_settings)
    stage = UpgradeStage(sqlite_after=ending != "prepared-cancel")
    try:
        before = await seed_cause_predecessor(database)
        task = asyncio.create_task(
            upgrade_database(empty_database_settings, stage.open)
        )
        try:
            await asyncio.wait_for(stage.entered.wait(), 4)
            if database.dialect is Dialect.POSTGRESQL and ending != "prepared-cancel":
                async with database.transaction() as holder:
                    await holder.exec_driver_sql(
                        "LOCK TABLE attempt IN SHARE UPDATE EXCLUSIVE MODE"
                    )
                    stage.release.set()
                    await wait_for_validation(database)
                    async with database.transaction() as progress:
                        await progress.exec_driver_sql(
                            "UPDATE attempt SET status = status"
                        )
                    if ending == "shutdown":
                        assert stage.database is not None
                        await stage.database.close(deadline_seconds=0)
                    else:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                if ending == "shutdown":
                    with pytest.raises(DatabaseClosedError):
                        await task
            elif ending == "shutdown":
                assert stage.database is not None
                await stage.database.close(deadline_seconds=0)
                stage.release.set()
                with pytest.raises(DatabaseClosedError):
                    await task
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            stage.release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
            columns = await connection.run_sync(
                lambda sync: inspect(sync).get_columns("attempt")
            )
            validated: object = None
            if database.dialect is Dialect.POSTGRESQL:
                validated = (
                    await connection.exec_driver_sql(
                        "SELECT convalidated FROM pg_constraint WHERE conname = "
                        "'ck_attempt_cancel_cause' AND conrelid = 'attempt'::regclass"
                    )
                ).scalar_one()
        locks = await migration_lock_count(database)
        recovered = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
            resumed = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )

        assert revision == "0003"
        assert after == resumed == before.history
        assert locks == 0
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            "0003",
            "0005",
            2,
        )
        assert differences == []
        assert stage.database is not None
        assert stage.ledger is not None
        assert stage.database.units_in_flight() == 0
        assert stage.database.writer_is_free()
        assert stage.ledger.opened == stage.ledger.closed
        assert stage.ledger.outstanding == stage.ledger.closed_while_checked_out == 0
        if database.dialect is Dialect.POSTGRESQL:
            assert validated is False
            assert stage.preparation_locks == [(0, 1)]
            assert any(column["name"] == "cancel_cause" for column in columns)
        else:
            assert stage.preparation_locks == []
            assert all(column["name"] != "cancel_cause" for column in columns)
    finally:
        if stage.database is not None:
            await stage.database.close()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["type", "not-null", "default", "domain"])
async def test_attempt_cancel_cause_upgrade_refuses_incompatible_schema(
    empty_database_settings: DatabaseSettings, shape: str
) -> None:
    database = Database(empty_database_settings)
    try:
        await seed_cause_predecessor(database)
        collation = '"C"' if database.dialect is Dialect.POSTGRESQL else '"BINARY"'
        declaration = (
            "INTEGER" if shape == "type" else f"VARCHAR(255) COLLATE {collation}"
        )
        if shape == "not-null":
            declaration += " NOT NULL DEFAULT 'cancel'"
        elif shape == "default":
            declaration += " DEFAULT 'cancel'"
        elif shape == "domain":
            declaration += (
                " CONSTRAINT ck_attempt_cancel_cause CHECK "
                "(cancel_cause IS NULL OR cancel_cause = 'other')"
            )
        async with database.transaction() as connection:
            await connection.exec_driver_sql(
                f"ALTER TABLE attempt ADD COLUMN cancel_cause {declaration}"
            )
            before = await connection.run_sync(capture_cause_predecessor)

        with pytest.raises((DBAPIError, RuntimeError)):
            await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
        locks = await migration_lock_count(database)

        assert revision == "0003"
        assert after == before.history
        assert locks == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_attempt_cancel_cause_upgrade_refusal_is_content_free(
    empty_database_settings: DatabaseSettings,
) -> None:
    marker = "PRIVATE-CANCEL-CAUSE-572"
    database = Database(empty_database_settings)
    try:
        await seed_cause_predecessor(database)
        async with database.transaction() as connection:
            if database.dialect is Dialect.POSTGRESQL:
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN cancel_cause "
                    'VARCHAR(255) COLLATE "C"'
                )
                await connection.execute(update(attempt).values(cancel_cause=marker))
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD CONSTRAINT ck_attempt_cancel_cause "
                    "CHECK (cancel_cause IS NULL OR cancel_cause IN "
                    "('cancel', 'clear', 'supersede', 'timeout', 'stop')) NOT VALID"
                )
            else:
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN cancel_cause "
                    f"VARCHAR(255) DEFAULT '{marker}'"
                )
        environment = {
            "KORA_DATABASE_URL": empty_database_settings.url.render_as_string(
                hide_password=False
            )
        }

        refused = await asyncio.to_thread(upgrade, environment)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            retained = (
                (await connection.execute(select(attempt.c.cancel_cause).distinct()))
                .scalars()
                .all()
            )
        locks = await migration_lock_count(database)

        assert refused.code == 70
        assert refused.stdout == b""
        assert marker not in refused.stderr.decode()
        assert events(refused).count("db.migration.failed") == 1
        assert "db.migration.finished" not in events(refused)
        assert revision == "0003"
        assert retained == [marker]
        assert locks == 0
    finally:
        await database.close()
