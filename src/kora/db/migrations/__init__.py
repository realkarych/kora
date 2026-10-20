from collections.abc import Callable
from time import perf_counter

from sqlalchemy import Connection

from kora.db import (
    DB_COMPONENT,
    DB_LOGGER_NAME,
    MIGRATION_FAILED,
    MIGRATION_FINISHED,
    MIGRATION_STARTED,
)
from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations.environment import (
    MigrationReport,
    alembic_config,
    bound_lock_waits,
    current_revision,
    head_revision,
    schema_differences,
    upgrade_to_head,
)
from kora.observability import ComponentLogger, current_diagnostics

BASE_REVISION: str = "base"


def migration_logger() -> ComponentLogger | None:
    diagnostics = current_diagnostics()
    if diagnostics is None:
        return None
    return diagnostics.logger(DB_COMPONENT, name=DB_LOGGER_NAME)


def upgrade_with_diagnostics(connection: Connection) -> MigrationReport:
    log = migration_logger()
    bound_lock_waits(connection)
    if log is not None:
        from_revision = current_revision(connection)
        log.emit(
            MIGRATION_STARTED,
            dialect=connection.dialect.name,
            from_revision=BASE_REVISION if from_revision is None else from_revision,
            to_revision=head_revision(alembic_config(connection)),
        )
    return upgrade_to_head(connection)


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


async def upgrade_database(
    settings: DatabaseSettings, open_database: OpenDatabase = Database
) -> MigrationReport:
    database: Database | None = None
    started = perf_counter()
    try:
        database = open_database(settings)
        async with database.transaction() as connection:
            report = await connection.run_sync(upgrade_with_diagnostics)
        return record_finished(report, started)
    except Exception as error:
        log = migration_logger()
        if log is not None:
            log.emit(MIGRATION_FAILED, dialect=str(settings.dialect), exception=error)
        raise
    finally:
        if database is not None:
            await database.close()


__all__ = [
    "MigrationReport",
    "current_revision",
    "schema_differences",
    "upgrade_database",
    "upgrade_to_head",
    "upgrade_with_diagnostics",
]
