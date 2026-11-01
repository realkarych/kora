import asyncio
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import insert, inspect, null, select, text, update
from sqlalchemy.exc import StatementError

from kora.db.connection import Database, DatabaseClosedError, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import DEADLINE_BINDING_CHECK, attempt
from kora.engine.deadline_records import (
    RecordedDeadlineBinding,
    UnrecordedDeadlineBinding,
    deadline_document,
    decode_deadline_record,
    prepare_deadline_record,
)
from tests.db.cancel_cause_support import (
    CausePredecessor,
    UpgradeStage,
    attempt_schema_objects,
    capture_cause_predecessor,
    migration_lock_count,
    seed_attempts,
    wait_for_validation,
)
from tests.db.seed import predecessor_snapshot
from tests.db.test_attempt_outcome_migration import seed_outcome_predecessor
from tests.db.test_db_upgrade_process import events, upgrade
from tests.db.test_migrations import differences_ignoring_expression_indexes
from tests.engine.test_deadline_records import AT, deadline

VALIDATE_DEADLINE: str = (
    "ALTER TABLE attempt VALIDATE CONSTRAINT ck_attempt_deadline_binding_object"
)


async def seed_deadline_predecessor(database: Database) -> CausePredecessor:
    await seed_outcome_predecessor(database)
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0005")
        )
    async with database.read_transaction() as connection:
        return await connection.run_sync(capture_cause_predecessor)


@pytest.mark.asyncio
async def test_deadline_migration_preserves_legacy_scalars(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await seed_deadline_predecessor(database)

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
                await connection.execute(
                    select(attempt.c.deadline_binding, attempt.c.deadline)
                )
            ).all()
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
        async with database.transaction() as connection:
            await connection.execute(
                insert(before.attempt_table).values(
                    **{**before.old_insert, "outcome": null()}
                )
            )
        async with database.read_transaction() as connection:
            omitted = (
                await connection.execute(
                    select(attempt.c.deadline_binding.is_(None)).where(
                        attempt.c.id == before.old_insert["id"]
                    )
                )
            ).scalar_one()
        noop = await upgrade_database(empty_database_settings)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0005",
            "0006",
            1,
        )
        assert after == before.history
        assert objects == before.schema_objects
        assert values
        assert all(
            isinstance(
                decode_deadline_record(row[0], row[1]), UnrecordedDeadlineBinding
            )
            for row in values
        )
        assert all(row[0] is None for row in values)
        assert omitted
        assert "deadline_binding" not in before.old_insert
        column = next(value for value in columns if value["name"] == "deadline_binding")
        assert column["nullable"]
        assert column.get("default") is None
        assert differences == []
        assert noop.steps == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deadline_binding_roundtrips_without_replacing_attempt_timing(
    database: Database,
) -> None:
    identifiers = await seed_attempts(database)
    original = deadline(timedelta.max)
    bounded = deadline_document(prepare_deadline_record(original))
    unlimited = deadline_document(prepare_deadline_record(None))
    async with database.transaction() as connection:
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == identifiers[0])
            .values(
                deadline=AT,
                deadline_binding=bounded,
                deadline_expired_at=AT,
                started_at=AT,
            )
        )
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == identifiers[1])
            .values(deadline_binding=unlimited)
        )

    async with database.read_transaction() as connection:
        rows = (
            await connection.execute(
                select(
                    attempt.c.deadline_binding,
                    attempt.c.deadline,
                    attempt.c.deadline_expired_at,
                    attempt.c.started_at,
                )
                .where(attempt.c.id.in_(identifiers[:2]))
                .order_by(attempt.c.number)
            )
        ).all()
    bound = decode_deadline_record(rows[0][0], rows[0][1])
    unbound = decode_deadline_record(rows[1][0], rows[1][1])

    assert bound == RecordedDeadlineBinding(original)
    assert unbound == RecordedDeadlineBinding(None)
    assert rows[0][2:] == (AT, AT)
    assert rows[1][2:] == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["null", "false", "3", "[]", '"private_marker"'])
async def test_deadline_storage_refuses_nonobject_json(
    database: Database, value: str
) -> None:
    identifier = (await seed_attempts(database))[0]
    statement = (
        "UPDATE attempt SET deadline_binding = CAST(:value AS JSONB), "
        "desired_state = 'cancel' WHERE id = :id"
        if database.dialect is Dialect.POSTGRESQL
        else "UPDATE attempt SET deadline_binding = :value, "
        "desired_state = 'cancel' WHERE id = :id"
    )

    with pytest.raises(StatementError):
        async with database.transaction() as connection:
            await connection.execute(
                text(statement),
                {"value": value, "id": identifier},
            )
    async with database.read_transaction() as connection:
        row = (
            await connection.execute(
                select(
                    attempt.c.deadline_binding.is_(None), attempt.c.desired_state
                ).where(attempt.c.id == identifier)
            )
        ).one()

    assert tuple(row) == (True, "run")


@pytest.mark.asyncio
async def test_deadline_storage_accepts_object_after_json_whitespace(
    database: Database,
) -> None:
    identifier = (await seed_attempts(database))[0]
    statement = (
        "UPDATE attempt SET deadline_binding = CAST(:value AS JSONB) WHERE id = :id"
        if database.dialect is Dialect.POSTGRESQL
        else "UPDATE attempt SET deadline_binding = :value WHERE id = :id"
    )
    value = ' \t\r\n{"version":1,"value":null}'

    async with database.transaction() as connection:
        await connection.execute(
            text(statement),
            {"value": value, "id": identifier},
        )
    async with database.read_transaction() as connection:
        saved = (
            await connection.execute(
                select(attempt.c.deadline_binding).where(attempt.c.id == identifier)
            )
        ).scalar_one()

    assert decode_deadline_record(saved, None) == RecordedDeadlineBinding(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["prepared-cancel", "validation-cancel", "shutdown"])
async def test_deadline_migration_recovers_partial_schema(
    empty_database_settings: DatabaseSettings, ending: str
) -> None:
    database = Database(empty_database_settings)
    stage = UpgradeStage(
        sqlite_after=ending != "prepared-cancel",
        column_name="deadline_binding",
        constraint_name="ck_attempt_deadline_binding_object",
    )
    try:
        before = await seed_deadline_predecessor(database)
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
                    await wait_for_validation(database, VALIDATE_DEADLINE)
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
        locks = await migration_lock_count(database)
        recovered = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )

        assert revision == "0005"
        assert after == before.history
        assert locks == 0
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            "0005",
            "0006",
            1,
        )
        assert differences == []
        assert stage.database is not None
        assert stage.ledger is not None
        assert stage.database.units_in_flight() == 0
        assert stage.ledger.outstanding == 0
        assert stage.ledger.opened == stage.ledger.closed
        assert stage.ledger.closed_while_checked_out == 0
        if database.dialect is Dialect.POSTGRESQL:
            assert stage.preparation_locks == [(0, 1)]
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["type", "not-null", "default", "domain"])
async def test_deadline_migration_refuses_incompatible_schema(
    empty_database_settings: DatabaseSettings, shape: str
) -> None:
    database = Database(empty_database_settings)
    try:
        await seed_deadline_predecessor(database)
        declaration = "JSONB" if database.dialect is Dialect.POSTGRESQL else "TEXT"
        if shape == "type":
            declaration = "INTEGER"
        elif shape == "not-null":
            declaration += " NOT NULL DEFAULT '{}'"
        elif shape == "default":
            declaration += " DEFAULT '{}'"
        elif shape == "domain":
            declaration += (
                " CONSTRAINT ck_attempt_deadline_binding_object "
                "CHECK (deadline_binding IS NULL)"
            )
        async with database.transaction() as connection:
            await connection.exec_driver_sql(
                f"ALTER TABLE attempt ADD COLUMN deadline_binding {declaration}"
            )
            before = await connection.run_sync(capture_cause_predecessor)

        with pytest.raises((StatementError, RuntimeError)):
            await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
        locks = await migration_lock_count(database)

        assert revision == "0005"
        assert after == before.history
        assert locks == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deadline_migration_refusal_is_content_free(
    empty_database_settings: DatabaseSettings,
) -> None:
    marker = "PRIVATE-DEADLINE-CONTENT-576"
    database = Database(empty_database_settings)
    try:
        await seed_deadline_predecessor(database)
        async with database.transaction() as connection:
            if database.dialect is Dialect.POSTGRESQL:
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN deadline_binding JSONB"
                )
                await connection.execute(
                    text("UPDATE attempt SET deadline_binding = CAST(:value AS JSONB)"),
                    {"value": f'"{marker}"'},
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD CONSTRAINT "
                    "ck_attempt_deadline_binding_object CHECK "
                    f"({DEADLINE_BINDING_CHECK}) NOT VALID"
                )
            else:
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN deadline_binding TEXT "
                    f"DEFAULT '\"{marker}\"'"
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
                (
                    await connection.execute(
                        select(attempt.c.deadline_binding).distinct()
                    )
                )
                .scalars()
                .all()
            )
        locks = await migration_lock_count(database)

        assert refused.code == 70
        assert refused.stdout == b""
        assert marker not in refused.stderr.decode()
        assert events(refused).count("db.migration.failed") == 1
        assert "db.migration.finished" not in events(refused)
        assert revision == "0005"
        assert retained == [marker]
        assert locks == 0
    finally:
        await database.close()
