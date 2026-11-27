import asyncio
from typing import cast

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.first_claim_basis import first_claim_basis_available
from kora.db.migrations import current_revision, database_is_at_head, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.run_event_page_v1 import (
    RUN_EVENT_REVISION,
    sqlite_run_event_page_available,
)
from kora.db.tables import attempt_dispatcher
from tests.db.test_db_upgrade_process import events, records, upgrade
from tests.engine.test_grant_history import legacy, seed
from tests.support.observability import run_python


async def populate_predecessor(database: Database) -> None:
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0033")
        )
    await legacy(database, await seed(database), range(1, 2))


async def native_catalog(connection: AsyncConnection) -> tuple[tuple[str, ...], bool]:
    tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    index = (
        connection.dialect.name == "sqlite"
        and await sqlite_run_event_page_available(connection)
    )
    return tuple(sorted(tables)), index


@pytest.mark.asyncio
async def test_run_event_metadata_upgrade_preserves_claim_receipt_and_qualifies(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        await populate_predecessor(database)
        assert not await database_is_at_head(database)
        async with database.read_snapshot() as reader:
            predecessor = await first_claim_basis_available(reader)
            original = (await reader.execute(select(attempt_dispatcher))).one()
            old_catalog = await native_catalog(reader)

        if database.dialect is Dialect.POSTGRESQL:
            await asyncio.to_thread(
                assert_postgres_migration_is_independent, empty_database_settings
            )
        else:
            migrated = await upgrade_database(empty_database_settings)
            repeated = await upgrade_database(empty_database_settings)
            assert (migrated.from_revision, migrated.to_revision, migrated.steps) == (
                "0033",
                RUN_EVENT_REVISION,
                1,
            )
            assert repeated.steps == 0
        assert await database_is_at_head(database)
        async with database.read_snapshot() as reader:
            revision = await reader.run_sync(current_revision)
            available = await first_claim_basis_available(reader)
            current = (await reader.execute(select(attempt_dispatcher))).one()
            new_catalog = await native_catalog(reader)

        assert predecessor is available is True
        assert revision == RUN_EVENT_REVISION
        assert current == original
        assert old_catalog == (new_catalog[0], False)
        assert new_catalog[1] is (database.dialect is Dialect.SQLITE)
    finally:
        await database.close()


POSTGRES_ORDINARY_MIGRATION = """
import asyncio
import os
import sys

import kora.__main__
from kora.db.connection import Database, Dialect, read_database_settings
from kora.db.migrations import database_is_at_head, upgrade_database
from kora.db.run_event_page_v1 import RUN_EVENT_REVISION

async def exercise() -> None:
    settings = read_database_settings(os.environ)
    assert settings.dialect is Dialect.POSTGRESQL
    database = Database(settings)
    try:
        assert not await database_is_at_head(database)
        report = await upgrade_database(settings)
        assert (report.from_revision, report.to_revision, report.steps) == (
            "0033", RUN_EVENT_REVISION, 1,
        )
        assert (await upgrade_database(settings)).steps == 0
        assert await database_is_at_head(database)
    finally:
        await database.close()

assert "kora.db.custody_native" not in sys.modules
asyncio.run(exercise())
assert "kora.db.custody_native" not in sys.modules
"""


def assert_postgres_migration_is_independent(settings: DatabaseSettings) -> None:
    result = run_python(
        ["-c", POSTGRES_ORDINARY_MIGRATION],
        {"KORA_DATABASE_URL": settings.url.render_as_string(hide_password=False)},
    )
    assert result.code == 0, result.stderr.decode()


@pytest.mark.asyncio
async def test_run_event_upgrade_refuses_exact_private_0034_and_preserves_database(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        await populate_predecessor(database)
        async with database.transaction() as writer:
            await writer.execute(
                text("UPDATE alembic_version SET version_num = '0034'")
            )
        async with database.read_snapshot() as reader:
            before = await native_catalog(reader)
            original = (await reader.execute(select(attempt_dispatcher))).one()

        result = await asyncio.to_thread(
            upgrade,
            {
                "KORA_DATABASE_URL": empty_database_settings.url.render_as_string(
                    hide_password=False
                ),
            },
        )
        async with database.read_snapshot() as reader:
            state = await native_catalog(reader)
            revision = await reader.run_sync(current_revision)
            current = (await reader.execute(select(attempt_dispatcher))).one()
            with pytest.raises(
                ClaimSourcesRejected, match="claim sources are unavailable"
            ):
                await first_claim_basis_available(reader)

        migration_failure = next(
            record
            for record in records(result)
            if record["event"] == "db.migration.failed"
        )
        chain = cast(list[dict[str, object]], migration_failure["exception"])
        assert any(item["type"] == "ResolutionError" for item in chain)
        assert "db.migration.finished" not in events(result)
        assert result.code == 70
        assert result.stdout == b""
        assert revision == "0034"
        assert state == before
        assert current == original
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sqlite", "postgresql"),
    [
        (
            "ALTER TABLE event DROP COLUMN actor",
            "ALTER TABLE event DROP COLUMN actor",
        ),
        (
            "DROP INDEX ix_event_run_page_bounded",
            "DROP INDEX ix_event_run_id_id",
        ),
        (
            "DROP INDEX ix_event_run_page_bounded",
            "ALTER TABLE event DROP CONSTRAINT ck_event_namespace_scope",
        ),
    ],
)
async def test_run_event_successor_claim_refuses_wrong_native_shape(
    database: Database,
    sqlite: str,
    postgresql: str,
) -> None:
    async with database.read_snapshot() as reader:
        assert await reader.run_sync(current_revision) == RUN_EVENT_REVISION
        assert await first_claim_basis_available(reader)
    async with database.transaction() as writer:
        if database.dialect is Dialect.SQLITE:
            await writer.execute(text("DROP INDEX ix_event_run_page_bounded"))
        if database.dialect is Dialect.POSTGRESQL or sqlite.startswith("ALTER"):
            await writer.execute(
                text(sqlite if database.dialect is Dialect.SQLITE else postgresql)
            )

    async with database.read_snapshot() as reader:
        with pytest.raises(ClaimSourcesRejected, match="claim sources are unavailable"):
            await first_claim_basis_available(reader)

        assert await reader.run_sync(current_revision) == RUN_EVENT_REVISION
