import asyncio
import sqlite3

import pytest
from sqlalchemy import CheckConstraint, inspect, select, text, update
from sqlalchemy.exc import DBAPIError

from kora.db.connection import (
    MIGRATION_LOCK_ID,
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    Dialect,
)
from kora.db.migrations import (
    MigrationReport,
    current_revision,
    upgrade_database,
    upgrade_to_head,
)
from kora.db.tables import metadata, price_table, usage_record
from tests.db.closing import until
from tests.db.predecessor import upgraded_predecessor
from tests.db.seed import START, predecessor_snapshot, seed_predecessor
from tests.db.test_migrations import differences_ignoring_expression_indexes


@pytest.mark.asyncio
async def test_price_schema_upgrade_preserves_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        async with database.transaction() as connection:
            before = await connection.run_sync(seed_predecessor)

        await database.close()
        report = await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.columns)
            )
            for table_name, fields in (
                ("price_table", "version, normalization_version"),
                (
                    "usage_record",
                    "normalization_version, reported_usd, known_token_fields, "
                    "cache_creation_by_ttl, observation_id, "
                    "observation_sequence, observed_at, dispatcher_id",
                ),
                ("attempt", "usage_coverage"),
                ("approval_request", "run_cost_version"),
            ):
                results = await connection.execute(
                    select(
                        *(
                            metadata.tables[table_name].c[name]
                            for name in fields.split(", ")
                        )
                    )
                )
                assert all(value is None for row in results for value in row)
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
            references = await connection.run_sync(
                lambda sync: inspect(sync).get_foreign_keys("attempt")
            )
        retry = await upgrade_database(empty_database_settings)

        assert report.to_revision == revision == "0002"
        assert after == before
        assert differences == []
        assert retry.steps == 0
        assert all(
            "price_table_id" not in item["constrained_columns"] for item in references
        )
    finally:
        await database.close()


def price_check() -> str:
    return str(
        next(
            item
            for item in price_table.constraints
            if isinstance(item, CheckConstraint)
        ).sqltext
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table_name", "column_name", "default"),
    [
        ("price_table", "model", "'unexpected-price-model'"),
        ("usage_record", "model", "'unexpected-usage-model'"),
        ("usage_record", "usd_estimate", "0"),
    ],
)
async def test_pricing_migration_refuses_altered_column_defaults(
    empty_database_settings: DatabaseSettings,
    table_name: str,
    column_name: str,
    default: str,
) -> None:
    database = Database(empty_database_settings)
    original_default: str | None = None
    try:
        async with database.transaction() as connection:
            before = await connection.run_sync(seed_predecessor)
            if database.dialect is Dialect.POSTGRESQL:
                await connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                    f"SET DEFAULT {default}"
                )
                original_default = await connection.run_sync(
                    lambda sync: next(
                        column["default"]
                        for column in inspect(sync).get_columns(table_name)
                        if column["name"] == column_name
                    )
                )
        await database.close()

        if empty_database_settings.dialect is Dialect.POSTGRESQL:
            with pytest.raises(RuntimeError, match="incompatible schema"):
                await upgrade_database(empty_database_settings)
            database = Database(empty_database_settings)
            async with database.transaction() as connection:
                revision = await connection.run_sync(current_revision)
                after = await connection.run_sync(
                    lambda sync: predecessor_snapshot(sync, before.columns)
                )
                retained_default = await connection.run_sync(
                    lambda sync: next(
                        column["default"]
                        for column in inspect(sync).get_columns(table_name)
                        if column["name"] == column_name
                    )
                )
                assert revision == "0001"
                assert after == before
                assert retained_default == original_default
                assert retained_default is not None
                await connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ALTER COLUMN {column_name} DROP DEFAULT"
                )
            await database.close()
        recovered = await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.transaction() as connection:
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.columns)
            )
            await connection.execute(
                usage_record.insert().values(
                    id="modern-unknown",
                    attempt_id="old-attempt",
                    model="modern-model",
                    input_tokens=0,
                    cached_input_tokens=0,
                    cache_creation_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=0,
                    completeness="partial",
                    basis="harness_list",
                    billing_class="api_key",
                    delta_of_session_totals=False,
                    source={},
                    normalization_version=1,
                    known_token_fields=[],
                    observation_id="unknown-cost-observation",
                    observation_sequence=0,
                    observed_at=START,
                    created_at=START,
                    updated_at=START,
                )
            )
            amounts = (
                await connection.execute(
                    select(
                        usage_record.c.usd_estimate, usage_record.c.reported_usd
                    ).where(usage_record.c.id == "modern-unknown")
                )
            ).one()

        assert recovered.to_revision == "0002"
        assert recovered.steps == 1
        assert after == before
        assert tuple(amounts) == (None, None)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_pricing_migration_recovers_after_failure(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        async with database.transaction() as connection:
            before = await connection.run_sync(seed_predecessor)
            if database.dialect is Dialect.SQLITE:
                await connection.exec_driver_sql(
                    "CREATE TABLE pricing_blocker (price_id VARCHAR(26) "
                    "REFERENCES price_table(id))"
                )
                await connection.exec_driver_sql(
                    "INSERT INTO pricing_blocker VALUES ('old-price')"
                )
            else:
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD COLUMN version INTEGER"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD COLUMN normalization_version INTEGER"
                )
                await connection.exec_driver_sql(
                    "UPDATE price_table SET version = 1 WHERE id = 'old-price'"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD CONSTRAINT "
                    f"ck_price_table_revision CHECK ({price_check()}) NOT VALID"
                )
        await database.close()

        with pytest.raises(DBAPIError):
            await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.transaction() as connection:
            revision = await connection.run_sync(current_revision)
            columns = await connection.run_sync(
                lambda sync: {
                    item["name"] for item in inspect(sync).get_columns("attempt")
                }
            )
            names = await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
            if database.dialect is Dialect.SQLITE:
                assert "usage_coverage" not in columns
                assert not any(name.startswith("_alembic_tmp") for name in names)
                await connection.exec_driver_sql("DROP TABLE pricing_blocker")
            else:
                assert "usage_coverage" in columns
                pending = (
                    await connection.execute(
                        text(
                            "SELECT convalidated FROM pg_constraint WHERE "
                            "conname = 'ck_price_table_revision'"
                        )
                    )
                ).scalar_one()
                assert pending is False
                await connection.exec_driver_sql(
                    "UPDATE price_table SET version = NULL WHERE id = 'old-price'"
                )
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.columns)
            )
        assert revision == "0001"
        assert after == before
        await database.close()

        recovered = await upgrade_database(empty_database_settings)
        again = await upgrade_database(empty_database_settings)

        assert recovered.to_revision == "0002"
        assert recovered.steps == 1
        assert again.steps == 0
    finally:
        await database.close()


async def retained_index_progress(settings: DatabaseSettings) -> None:
    database = Database(settings)
    try:
        async with database.transaction() as connection:
            await connection.run_sync(seed_predecessor)
            await connection.exec_driver_sql(
                "CREATE INDEX ix_price_table_default_effective ON price_table (model)"
            )
        with pytest.raises(RuntimeError, match="incompatible schema"):
            await upgrade_database(settings)
        async with database.transaction() as connection:
            assert await connection.run_sync(current_revision) == "0001"
            valid = (
                await connection.execute(
                    text(
                        "SELECT bool_and(convalidated) FROM pg_constraint WHERE "
                        "conname IN ('ck_price_table_revision', "
                        "'ck_usage_record_normalization', "
                        "'ck_approval_request_run_cost_version')"
                    )
                )
            ).scalar_one()
            assert valid is True
            await connection.exec_driver_sql(
                "DROP INDEX ix_price_table_default_effective"
            )
    finally:
        await database.close()


async def wait_for_index(database: Database) -> int:
    async with asyncio.timeout(4):
        while True:
            async with database.read_transaction() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT c.oid FROM pg_class c JOIN pg_index i ON "
                            "c.oid = i.indexrelid WHERE c.relname = "
                            "'ix_price_table_default_effective' AND NOT "
                            "i.indisvalid"
                        )
                    )
                ).first()
            if row is not None:
                return int(row[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["lock-timeout", "cancelled"])
async def test_pricing_migration_recovers_after_index_interruption(
    empty_database_settings: DatabaseSettings, cancel: bool
) -> None:
    if empty_database_settings.dialect is Dialect.SQLITE:
        database = Database(empty_database_settings)
        try:
            async with database.transaction() as connection:
                await connection.run_sync(seed_predecessor)
            async with database.transaction() as holder:
                await holder.exec_driver_sql("UPDATE price_table SET model = model")
                opened: list[Database] = []

                def open_contender(settings: DatabaseSettings) -> Database:
                    contender = Database(settings)
                    opened.append(contender)
                    return contender

                task = asyncio.create_task(
                    upgrade_database(empty_database_settings, open_contender)
                )
                if cancel:
                    await until(
                        lambda: bool(opened) and opened[0].units_in_flight() == 1
                    )
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises((DBAPIError, sqlite3.OperationalError)):
                        await task
            recovered = await upgrade_database(empty_database_settings)
            assert recovered.to_revision == "0002"
        finally:
            await database.close()
        return
    await retained_index_progress(empty_database_settings)
    database = Database(empty_database_settings)
    try:
        async with database.transaction() as holder:
            await holder.exec_driver_sql(
                "UPDATE price_table SET model = model WHERE id = 'old-price'"
            )
            task = asyncio.create_task(upgrade_database(empty_database_settings))
            invalid_oid = await wait_for_index(database)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(DBAPIError, match="lock timeout"):
                    await task
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            old_index = (
                await connection.execute(
                    text("SELECT indisvalid FROM pg_index WHERE indexrelid = :oid"),
                    {"oid": invalid_oid},
                )
            ).scalar_one()
            locks = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = "
                        "'advisory' AND objid = :key"
                    ),
                    {"key": MIGRATION_LOCK_ID},
                )
            ).scalar_one()

        recovered = await upgrade_database(empty_database_settings)
        again = await upgrade_database(empty_database_settings)

        assert revision == "0001"
        assert old_index is False
        assert locks == 0
        assert recovered.to_revision == "0002"
        assert again.steps == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_pricing_upgrades_are_serialized(
    empty_database_settings: DatabaseSettings,
) -> None:
    if empty_database_settings.dialect is Dialect.SQLITE:
        results = await asyncio.gather(
            upgrade_database(empty_database_settings),
            upgrade_database(empty_database_settings),
            return_exceptions=True,
        )
        completed = [
            report for report in results if isinstance(report, MigrationReport)
        ]
        refused = [error for error in results if isinstance(error, BaseException)]
        assert any(report.steps == 2 for report in completed)
        assert all(
            isinstance(error, (DBAPIError, sqlite3.OperationalError))
            for error in refused
        )
        assert (await upgrade_database(empty_database_settings)).steps == 0
        return
    await retained_index_progress(empty_database_settings)
    database = Database(empty_database_settings)
    try:
        async with database.transaction() as holder:
            await holder.exec_driver_sql(
                "UPDATE price_table SET model = model WHERE id = 'old-price'"
            )
            first = asyncio.create_task(upgrade_database(empty_database_settings))
            original_oid = await wait_for_index(database)
            second = asyncio.create_task(upgrade_database(empty_database_settings))
            async with asyncio.timeout(4):
                waiting = 0
                while not waiting:
                    async with database.read_transaction() as connection:
                        waiting = (
                            await connection.execute(
                                text(
                                    "SELECT count(*) FROM pg_locks WHERE "
                                    "locktype = 'advisory' AND objid = :key AND "
                                    "NOT granted"
                                ),
                                {"key": MIGRATION_LOCK_ID},
                            )
                        ).scalar_one()
            assert await wait_for_index(database) == original_oid
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        finished = await second
        again = await upgrade_database(empty_database_settings)

        assert finished.to_revision == "0002"
        assert again.steps == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_migration_lease_closed_before_stamp_keeps_predecessor(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    async with database.transaction() as connection:
        await connection.run_sync(seed_predecessor)

    with pytest.raises(DatabaseClosedError):
        await close_before_migration(database)
    observer = Database(empty_database_settings)
    try:
        async with observer.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            if observer.dialect is Dialect.POSTGRESQL:
                locks = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = "
                            "'advisory' AND objid = :key"
                        ),
                        {"key": MIGRATION_LOCK_ID},
                    )
                ).scalar_one()
                assert locks == 0

        assert revision == "0001"
        assert database.units_in_flight() == 0
    finally:
        await observer.close()
        await database.close()


@pytest.mark.asyncio
async def test_pricing_tags_reject_partial_or_out_of_domain_rows(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = await upgraded_predecessor(empty_database_settings)
    try:
        rejected = [
            "UPDATE price_table SET version = 1 WHERE id = 'old-price'",
            "UPDATE price_table SET normalization_version = 1 WHERE id = 'old-price'",
            "UPDATE price_table SET version = 2, normalization_version = 2, "
            "model = NULL WHERE id = 'old-price'",
            "UPDATE usage_record SET normalization_version = 1 WHERE id = 'old-zero'",
            "UPDATE usage_record SET observation_id = 'partial' WHERE id = 'old-zero'",
            "UPDATE usage_record SET normalization_version = 2 WHERE id = 'old-zero'",
            "UPDATE approval_request SET run_cost_version = 2 WHERE id = "
            "'old-approval'",
        ]

        for statement in rejected:
            with pytest.raises(DBAPIError):
                async with database.transaction() as connection:
                    await connection.exec_driver_sql(statement)
        async with database.transaction() as connection:
            await connection.execute(
                update(usage_record)
                .where(usage_record.c.id == "old-zero")
                .values(
                    normalization_version=1,
                    known_token_fields=[],
                    observation_id="observed",
                    observation_sequence=0,
                    observed_at=START,
                )
            )
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            for value in (-1, 2147483648):
                with pytest.raises(DBAPIError):
                    async with database.transaction() as connection:
                        await connection.execute(
                            update(usage_record)
                            .where(usage_record.c.id == "old-zero")
                            .values({field: value}),
                        )
        if database.dialect is Dialect.SQLITE:
            with pytest.raises(DBAPIError):
                async with database.transaction() as connection:
                    await connection.exec_driver_sql(
                        "UPDATE usage_record SET input_tokens = 1.5 WHERE id = "
                        "'old-zero'"
                    )

        async with database.read_transaction() as connection:
            values = (
                await connection.execute(
                    text(
                        "SELECT input_tokens, observation_sequence FROM "
                        "usage_record WHERE id = 'old-zero'"
                    )
                )
            ).one()
        assert tuple(values) == (0, 0)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_migration_lease_restores_session_and_releases_lock(
    database: Database,
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.migration_connection() as connection:
            assert database.units_in_flight() == 1
            assert (
                await connection.exec_driver_sql("PRAGMA foreign_keys")
            ).scalar_one() == 1
        assert database.units_in_flight() == 0
        return
    async with database.transaction() as connection:
        await connection.exec_driver_sql("SET lock_timeout = '17s'")
        backend = (
            await connection.exec_driver_sql("SELECT pg_backend_pid()")
        ).scalar_one()

    async with database.migration_connection() as connection:
        assert database.units_in_flight() == 1
        assert (
            await connection.exec_driver_sql("SHOW lock_timeout")
        ).scalar_one() == "5s"
        await connection.rollback()
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.exec_driver_sql("SELECT 1")
    async with database.transaction() as connection:
        after_backend = (
            await connection.exec_driver_sql("SELECT pg_backend_pid()")
        ).scalar_one()
        timeout = (await connection.exec_driver_sql("SHOW lock_timeout")).scalar_one()
        isolation = (
            await connection.exec_driver_sql("SHOW transaction_isolation")
        ).scalar_one()
        locks = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid()"
                )
            )
        ).scalar_one()
        await connection.exec_driver_sql("RESET lock_timeout")

    assert after_backend == backend
    assert timeout == "17s"
    assert isolation == "read committed"
    assert locks == 0
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_competing_migration_lock_wait_is_bounded(database: Database) -> None:
    if database.dialect is Dialect.SQLITE:
        assert (await upgrade_database(database.settings)).steps == 0
        return
    contender = Database(database.settings)
    try:
        async with database.migration_connection():
            with pytest.raises(DBAPIError, match="lock timeout"):
                async with contender.migration_connection():
                    pytest.fail("a contender entered a held migration session")
        async with contender.migration_connection() as connection:
            report = await connection.run_sync(upgrade_to_head)

        assert report.steps == 0
        assert contender.units_in_flight() == 0
    finally:
        await contender.close()


async def close_before_migration(database: Database) -> None:
    async with database.migration_connection() as connection:
        assert database.units_in_flight() == 1
        await database.close(deadline_seconds=0)
        await connection.run_sync(upgrade_to_head)
