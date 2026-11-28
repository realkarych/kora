import asyncio
import stat
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from sqlalchemy import Connection

from kora.db import (
    DB_COMPONENT,
    DB_LOGGER_NAME,
    MIGRATION_FAILED,
    MIGRATION_FINISHED,
    MIGRATION_STARTED,
)
from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations.environment import (
    HEAD,
    MigrationReport,
    alembic_config,
    bound_lock_waits,
    current_revision,
    head_revision,
    prepared_position_catalog,
    schema_differences,
    upgrade_to_head,
)
from kora.db.run_event_maintenance_admission import (
    SQLITE_FRESH_EMPTY,
    PositionMaintenanceRefused,
    earlier_position_target,
    sqlite_has_sidecars,
    sqlite_main_admit,
    sqlite_snapshot,
)
from kora.db.run_event_position_v1 import POSITION_REVISION
from kora.observability import ComponentLogger, current_diagnostics
from kora.sqlite.driver import Admission, NativeRetirement
from kora.sqlite.driver import Error as NativeSQLiteError

BASE_REVISION: str = "base"


def migration_logger() -> ComponentLogger | None:
    diagnostics = current_diagnostics()
    if diagnostics is None:
        return None
    return diagnostics.logger(DB_COMPONENT, name=DB_LOGGER_NAME)


def upgrade_with_diagnostics(
    connection: Connection, target: str = HEAD
) -> MigrationReport:
    log = migration_logger()
    bound_lock_waits(connection)
    if log is not None:
        from_revision = current_revision(connection)
        log.emit(
            MIGRATION_STARTED,
            dialect=connection.dialect.name,
            from_revision=BASE_REVISION if from_revision is None else from_revision,
            to_revision=head_revision(alembic_config(connection))
            if target == HEAD
            else target,
        )
    return upgrade_to_head(connection, target)


def record_finished(report: MigrationReport, started: float) -> MigrationReport:
    log = migration_logger()
    if log is not None:
        log.emit(
            MIGRATION_FINISHED,
            dialect=report.dialect,
            revision=report.to_revision,
            steps=report.steps,
            duration_ms=(perf_counter() - started) * 1000,
        )
    return report


OpenDatabase = Callable[[DatabaseSettings], Database]


def is_current_revision(connection: Connection) -> bool:
    return current_revision(connection) == head_revision(alembic_config(connection))


async def database_is_at_head(database: Database) -> bool:
    async with database.read_transaction() as connection:
        return await connection.run_sync(is_current_revision)


def sqlite_preflight(path: Path) -> Admission | MigrationReport | None:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(existing.st_mode):
        raise PositionMaintenanceRefused
    if not existing.st_size:
        return None
    try:
        canonical = path.resolve(strict=True)
        admit = sqlite_main_admit(canonical)
        live = sqlite_has_sidecars(canonical)

        def inspect(connection: Connection) -> Admission | MigrationReport:
            if current_revision(
                connection
            ) == POSITION_REVISION and prepared_position_catalog(connection):
                return MigrationReport(
                    "sqlite", POSITION_REVISION, POSITION_REVISION, 0
                )
            if not live and connection.scalar(SQLITE_FRESH_EMPTY) in (True, 1):
                return admit
            raise PositionMaintenanceRefused

        return sqlite_snapshot(canonical, admit, inspect)
    except (OSError, NativeSQLiteError):
        raise PositionMaintenanceRefused from None


async def upgrade_database(
    settings: DatabaseSettings,
    open_database: OpenDatabase = Database,
    *,
    target: str = HEAD,
) -> MigrationReport:
    database: Database | None = None
    started = perf_counter()
    try:
        existing = None
        qualify_sqlite = (
            settings.dialect is Dialect.SQLITE and not earlier_position_target(target)
        )
        if qualify_sqlite:
            existing = await asyncio.to_thread(
                sqlite_preflight, settings.sqlite_path.absolute()
            )
        if isinstance(existing, MigrationReport):
            return record_finished(existing, started)
        database = open_database(settings)
        async with database.migration_connection(
            target=target,
            sqlite_admit=existing,
            sqlite_existing_empty=existing is not None,
        ) as connection:
            report = await connection.run_sync(upgrade_with_diagnostics, target)
    except Exception as error:
        log = migration_logger()
        if log is not None:
            log.emit(MIGRATION_FAILED, dialect=str(settings.dialect), exception=error)
        raise
    finally:
        if database is not None:
            await database.close()
    if qualify_sqlite:
        if not NativeRetirement.drain(settings.sqlite_path.absolute()):
            raise PositionMaintenanceRefused
    return record_finished(report, started)


__all__ = [
    "MigrationReport",
    "current_revision",
    "database_is_at_head",
    "schema_differences",
    "upgrade_database",
    "upgrade_to_head",
    "upgrade_with_diagnostics",
]
