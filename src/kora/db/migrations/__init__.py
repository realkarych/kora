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
    dialect = connection.dialect.name
    if log is not None:
        from_revision = current_revision(connection)
        log.emit(
            MIGRATION_STARTED,
            dialect=dialect,
            from_revision=BASE_REVISION if from_revision is None else from_revision,
            to_revision=head_revision(alembic_config(connection)),
        )
    report = upgrade_to_head(connection)
    if log is not None:
        log.emit(
            MIGRATION_FINISHED,
            dialect=dialect,
            revision=report.to_revision,
            steps=report.steps,
            duration_ms=report.duration_ms,
        )
    return report


async def upgrade_database(settings: DatabaseSettings) -> MigrationReport:
    database = Database(settings)
    try:
        async with database.transaction() as connection:
            return await connection.run_sync(upgrade_with_diagnostics)
    except Exception as error:
        log = migration_logger()
        if log is not None:
            log.emit(MIGRATION_FAILED, dialect=str(settings.dialect), exception=error)
        raise
    finally:
        await database.close()


__all__ = [
    "MigrationReport",
    "current_revision",
    "schema_differences",
    "upgrade_database",
    "upgrade_to_head",
    "upgrade_with_diagnostics",
]
