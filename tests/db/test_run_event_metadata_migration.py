import asyncio
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, DatabaseSettings, Dialect, parse_database_url
from kora.db.first_claim_basis import first_claim_basis_available
from kora.db.migrations import current_revision, database_is_at_head, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.run_event_maintenance_admission import (
    PositionMaintenanceRefused,
    sqlite_main_admit,
    sqlite_snapshot,
)
from kora.db.run_event_page_v1 import (
    RUN_EVENT_REVISION,
    sqlite_run_event_page_available,
)
from kora.db.tables import attempt_dispatcher
from kora.sqlite.driver import Connection as NativeConnection
from kora.sqlite.driver import NativeRetirement
from tests.db.predecessor import PredecessorDbUpgrade, upgrade_to_run_event_metadata
from tests.db.test_db_upgrade_process import events, records, upgrade
from tests.engine.test_grant_history import legacy, seed
from tests.support.observability import run_python


async def populate_predecessor(database: Database) -> None:
    async with database.migration_connection(target="0033") as connection:
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
            migrated = await upgrade_to_run_event_metadata(empty_database_settings)
            repeated = await upgrade_to_run_event_metadata(empty_database_settings)
            assert (migrated.from_revision, migrated.to_revision, migrated.steps) == (
                "0033",
                RUN_EVENT_REVISION,
                1,
            )
            assert repeated.steps == 0
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
from kora.db.migrations import current_revision, database_is_at_head, upgrade_database
from kora.db.run_event_page_v1 import RUN_EVENT_REVISION

async def exercise() -> None:
    settings = read_database_settings(os.environ)
    assert settings.dialect is Dialect.POSTGRESQL
    database = Database(settings)
    try:
        assert not await database_is_at_head(database)
        report = await upgrade_database(settings, target=RUN_EVENT_REVISION)
        assert (report.from_revision, report.to_revision, report.steps) == (
            "0033", RUN_EVENT_REVISION, 1,
        )
        assert (await upgrade_database(settings, target=RUN_EVENT_REVISION)).steps == 0
        async with database.read_transaction() as reader:
            assert await reader.run_sync(current_revision) == RUN_EVENT_REVISION
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


def sqlite_images(path: Path) -> dict[str, tuple[bytes, int, int, int, int]]:
    return {
        item.name: (
            item.read_bytes(),
            native.st_ctime_ns,
            native.st_mode,
            native.st_dev,
            native.st_ino,
        )
        for item in path.parent.iterdir()
        if item.name
        in {path.name, *(path.name + suffix for suffix in ("-wal", "-shm", "-journal"))}
        for native in (item.stat(),)
    }


@pytest.mark.asyncio
async def test_ownerless_probe_retains_scope_until_incumbent_releases(
    tmp_path: Path,
) -> None:
    path = tmp_path.resolve() / "original.db"
    await upgrade_database(parse_database_url(f"sqlite+aiosqlite:///{path}"))
    admit = sqlite_main_admit(path)
    with closing(NativeConnection()) as incumbent:
        incumbent.open(path, False, 0, admit, snapshot="exclusive")
        with pytest.raises(PositionMaintenanceRefused):
            sqlite_snapshot(path, admit, current_revision)
        assert not NativeRetirement.drain(path)
    assert NativeRetirement.drain(path)


@pytest.mark.asyncio
async def test_refusing_legacy_preflight_preserves_existing_empty_companions(
    tmp_path: Path,
) -> None:
    path = tmp_path.resolve() / "original.db"
    address = f"sqlite+aiosqlite:///{path}"
    await upgrade_database(parse_database_url(address), target="0033")
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        sidecar.touch(mode=0o640, exist_ok=False)
        sidecar.chmod(0o640)
    before = sqlite_images(path)
    refusal = await asyncio.to_thread(upgrade, {"KORA_DATABASE_URL": address})
    assert refusal.code == 78
    assert refusal.stdout == b""
    assert sqlite_images(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor", ["0032", "0033", RUN_EVENT_REVISION])
async def test_position_target_refuses_earlier_catalog_before_native_effects(
    empty_database_settings: DatabaseSettings,
    predecessor: str,
) -> None:
    settings = empty_database_settings
    await upgrade_database(settings, target=predecessor)
    database = Database(settings)
    try:
        await legacy(database, await seed(database), range(1, 2))
        async with database.read_snapshot() as reader:
            original = (await reader.execute(select(attempt_dispatcher))).all()
            catalog = await native_catalog(reader)
    finally:
        await database.close()

    sqlite = settings.sqlite_path if settings.dialect is Dialect.SQLITE else None
    images = None if sqlite is None else sqlite_images(sqlite)
    result = await asyncio.to_thread(
        upgrade,
        {"KORA_DATABASE_URL": settings.url.render_as_string(hide_password=False)},
    )
    assert result.code == 78
    assert not result.stdout
    assert "db.migration.finished" not in events(result)
    database = Database(settings)
    try:
        with pytest.raises(PositionMaintenanceRefused):
            async with database.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.upgrade(alembic_config(sync), "head")
                )
        if sqlite is not None:
            assert sqlite_images(sqlite) == images
        async with database.read_snapshot() as reader:
            assert await reader.run_sync(current_revision) == predecessor
            assert await native_catalog(reader) == catalog
            assert (await reader.execute(select(attempt_dispatcher))).all() == original
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_event_upgrade_refuses_exact_private_0034_and_preserves_database(
    empty_database_settings: DatabaseSettings,
    predecessor_db_upgrade: PredecessorDbUpgrade,
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
            predecessor_db_upgrade,
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
    empty_database_settings: DatabaseSettings,
    sqlite: str,
    postgresql: str,
) -> None:
    await upgrade_to_run_event_metadata(empty_database_settings)
    database = Database(empty_database_settings)
    try:
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
            with pytest.raises(
                ClaimSourcesRejected, match="claim sources are unavailable"
            ):
                await first_claim_basis_available(reader)

            assert await reader.run_sync(current_revision) == RUN_EVENT_REVISION
    finally:
        await database.close()
