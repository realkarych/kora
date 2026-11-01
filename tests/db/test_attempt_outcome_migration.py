import asyncio

import pytest
from alembic import command
from sqlalchemy import insert, inspect, select, update
from sqlalchemy.exc import StatementError

from kora.db.connection import Database, DatabaseClosedError, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import OUTCOME_PRESENCE_CHECK, attempt
from kora.engine.outcome_records import (
    RecordedOutcome,
    UnavailableOutcomeError,
    decode_outcome_record,
    receipt_document,
)
from tests.db.cancel_cause_support import (
    CausePredecessor,
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
from tests.engine.test_outcome_records import prepared

VALIDATE_OUTCOME: str = (
    "ALTER TABLE attempt VALIDATE CONSTRAINT ck_attempt_outcome_payload_presence"
)


async def seed_outcome_predecessor(database: Database) -> CausePredecessor:
    await seed_cause_predecessor(database, rich=True)
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0004")
        )
    async with database.transaction() as connection:
        await connection.execute(
            update(attempt)
            .where(attempt.c.status == "finishing")
            .values(cancel_cause="stop")
        )
        return await connection.run_sync(capture_cause_predecessor)


@pytest.mark.asyncio
async def test_outcome_migration_preserves_unrecorded_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await seed_outcome_predecessor(database)

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
                    select(
                        attempt.c.outcome,
                        attempt.c.outcome_payload,
                        attempt.c.outcome_payload_state,
                    )
                )
            ).all()
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
                    select(
                        attempt.c.outcome,
                        attempt.c.outcome_payload,
                        attempt.c.outcome_payload_state,
                    ).where(attempt.c.id == before.old_insert["id"])
                )
            ).one()
        noop = await upgrade_database(empty_database_settings)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0004",
            "0005",
            1,
        )
        assert after == before.history
        assert objects == before.schema_objects
        assert values
        assert all(tuple(value) == (None, None, None) for value in values)
        assert tuple(omitted) == (None, None, None)
        for name in ("outcome", "outcome_payload", "outcome_payload_state"):
            column = next(value for value in columns if value["name"] == name)
            assert column["nullable"]
            assert column.get("default") is None
        assert differences == []
        assert noop.steps == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_original_outcome_body_roundtrips_and_expiry_keeps_identity(
    database: Database,
) -> None:
    identifier = (await seed_attempts(database))[0]
    record = prepared(
        {"\u0000": "private_marker\u0000", "literal": "\\u0000", "number": 1.0}
    )
    async with database.read_transaction() as connection:
        unrecorded = (
            await connection.execute(
                select(attempt.c.outcome.is_(None)).where(attempt.c.id == identifier)
            )
        ).scalar_one()
        assert unrecorded

    async with database.transaction() as connection:
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == identifier)
            .values(
                outcome=receipt_document(record.receipt),
                outcome_payload=record.payload,
                outcome_payload_state=record.payload_state,
            )
        )
    async with database.read_transaction() as connection:
        saved = (
            await connection.execute(
                select(
                    attempt.c.outcome,
                    attempt.c.outcome_payload,
                    attempt.c.outcome_payload_state,
                ).where(attempt.c.id == identifier)
            )
        ).one()
    async with database.transaction() as connection:
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == identifier)
            .values(
                status="failed",
                error_class="timeout",
                outcome_payload=None,
                outcome_payload_state="expired",
            )
        )
    async with database.read_transaction() as connection:
        expired = (
            await connection.execute(
                select(
                    attempt.c.outcome,
                    attempt.c.outcome_payload,
                    attempt.c.outcome_payload_state,
                ).where(attempt.c.id == identifier)
            )
        ).one()
    decoded = decode_outcome_record(expired[0], expired[1], expired[2])

    assert saved[0] == receipt_document(record.receipt)
    assert saved[1] == record.payload
    assert saved[2] == "available"
    assert isinstance(decoded, RecordedOutcome)
    assert isinstance(decoded.content, UnavailableOutcomeError)
    assert decoded.receipt == record.receipt
    assert decoded.receipt.error_class == "command_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["prepared-cancel", "validation-cancel", "shutdown"])
async def test_outcome_migration_recovers_partial_schema(
    empty_database_settings: DatabaseSettings, ending: str
) -> None:
    database = Database(empty_database_settings)
    stage = UpgradeStage(
        sqlite_after=ending != "prepared-cancel",
        column_name="outcome_payload_state",
        constraint_name="ck_attempt_outcome_payload_presence",
    )
    try:
        before = await seed_outcome_predecessor(database)
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
                    await wait_for_validation(database, VALIDATE_OUTCOME)
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

        assert revision == "0004"
        assert after == before.history
        assert locks == 0
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            "0004",
            "0005",
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
@pytest.mark.parametrize(
    ("has_receipt", "payload", "state"),
    [
        (False, "{}", None),
        (False, None, "not_applicable"),
        (True, None, None),
        (True, None, "available"),
        (True, "{}", "expired"),
        (True, "{}", "missing"),
        (True, "{}", "not_applicable"),
        (True, None, "AVAILABLE"),
    ],
)
async def test_outcome_storage_refuses_inconsistent_presence(
    database: Database, has_receipt: bool, payload: str | None, state: str | None
) -> None:
    identifier = (await seed_attempts(database))[0]
    record = prepared({})
    async with database.read_transaction() as connection:
        unrecorded = (
            await connection.execute(
                select(attempt.c.outcome.is_(None)).where(attempt.c.id == identifier)
            )
        ).scalar_one()
        assert unrecorded

    with pytest.raises(StatementError):
        async with database.transaction() as connection:
            await connection.execute(
                update(attempt)
                .where(attempt.c.id == identifier)
                .values(
                    outcome=receipt_document(record.receipt) if has_receipt else None,
                    outcome_payload=payload,
                    outcome_payload_state=state,
                    desired_state="cancel",
                )
            )
    async with database.read_transaction() as connection:
        desired = (
            await connection.execute(
                select(attempt.c.desired_state).where(attempt.c.id == identifier)
            )
        ).scalar_one()

    assert desired == "run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "shape"),
    [
        ("outcome", "type"),
        ("outcome_payload", "type"),
        ("outcome_payload_state", "type"),
        ("outcome", "not-null"),
        ("outcome_payload", "default"),
        ("outcome_payload_state", "domain"),
    ],
)
async def test_outcome_migration_refuses_incompatible_schema(
    empty_database_settings: DatabaseSettings, column: str, shape: str
) -> None:
    database = Database(empty_database_settings)
    try:
        await seed_outcome_predecessor(database)
        header_type = "JSONB" if database.dialect is Dialect.POSTGRESQL else "TEXT"
        collation = '"C"' if database.dialect is Dialect.POSTGRESQL else '"BINARY"'
        declarations = {
            "outcome": header_type,
            "outcome_payload": "TEXT",
            "outcome_payload_state": f"VARCHAR(255) COLLATE {collation}",
        }
        if shape == "type":
            declarations[column] = "INTEGER"
        elif shape == "not-null":
            declarations[column] += " NOT NULL DEFAULT '{}'"
        elif shape == "default":
            declarations[column] += " DEFAULT 'private_marker'"
        async with database.transaction() as connection:
            for name, declaration in declarations.items():
                suffix = ""
                if name == "outcome_payload_state" and shape == "domain":
                    suffix = (
                        " CONSTRAINT ck_attempt_outcome_payload_presence "
                        "CHECK (outcome_payload_state IS NULL OR "
                        "outcome_payload_state = 'other')"
                    )
                await connection.exec_driver_sql(
                    f"ALTER TABLE attempt ADD COLUMN {name} {declaration}{suffix}"
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

        assert revision == "0004"
        assert after == before.history
        assert locks == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_outcome_migration_refusal_is_content_free(
    empty_database_settings: DatabaseSettings,
) -> None:
    marker = "PRIVATE-OUTCOME-CONTENT-575"
    database = Database(empty_database_settings)
    try:
        await seed_outcome_predecessor(database)
        async with database.transaction() as connection:
            if database.dialect is Dialect.POSTGRESQL:
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN outcome JSONB"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN outcome_payload TEXT"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN outcome_payload_state "
                    'VARCHAR(255) COLLATE "C"'
                )
                await connection.execute(
                    update(attempt).values(
                        outcome_payload=marker, outcome_payload_state="available"
                    )
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD CONSTRAINT "
                    "ck_attempt_outcome_payload_presence CHECK "
                    f"({OUTCOME_PRESENCE_CHECK}) NOT VALID"
                )
            else:
                await connection.exec_driver_sql(
                    "ALTER TABLE attempt ADD COLUMN outcome_payload TEXT "
                    f"DEFAULT '{marker}'"
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
                (await connection.execute(select(attempt.c.outcome_payload).distinct()))
                .scalars()
                .all()
            )
        locks = await migration_lock_count(database)

        assert refused.code == 70
        assert refused.stdout == b""
        assert marker not in refused.stderr.decode()
        assert events(refused).count("db.migration.failed") == 1
        assert "db.migration.finished" not in events(refused)
        assert revision == "0004"
        assert retained == [marker]
        assert locks == 0
    finally:
        await database.close()
