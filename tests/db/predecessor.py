from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, MetaData

from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import MigrationReport, current_revision, upgrade_database
from kora.db.migrations.environment import column_types_differ, include_schema_object
from kora.db.run_event_page_v1 import RUN_EVENT_REVISION
from kora.db.run_event_position_v1 import (
    POSITION_TABLE,
    POSITION_TRANSITION,
    RUN_POSITION_STATE,
)
from kora.db.tables import metadata
from tests.db.closing import legacy_preparation
from tests.db.seed import seed_predecessor
from tests.support.observability import ProcessResult
from tests.support.warning_filters import expression_index_warnings_ignored

PredecessorDbUpgrade = Callable[[Mapping[str, str]], ProcessResult]
PredecessorProducer = Literal["promotion", "admission", "capacity", "deadline"]
PredecessorWrite = Callable[[DatabaseSettings, PredecessorProducer], Awaitable[str]]


@dataclass(frozen=True)
class PredecessorInstallation:
    python: Path
    source: Path
    wheel: Path


async def upgrade_to_run_event_metadata(
    settings: DatabaseSettings,
    open_database: Callable[[DatabaseSettings], Database] = Database,
) -> MigrationReport:
    return await upgrade_database(settings, open_database, target=RUN_EVENT_REVISION)


def historical_schema_differences(connection: Connection) -> list[str]:
    assert current_revision(connection) == RUN_EVENT_REVISION
    expected = MetaData(naming_convention=metadata.naming_convention)
    for table in metadata.tables.values():
        if table.name not in (POSITION_TABLE, POSITION_TRANSITION, RUN_POSITION_STATE):
            table.to_metadata(expected)
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": column_types_differ,
            "include_object": partial(include_schema_object, connection.dialect.name),
        },
    )
    with expression_index_warnings_ignored():
        return [str(difference) for difference in compare_metadata(context, expected)]


async def upgraded_predecessor(settings: DatabaseSettings) -> Database:
    seeded = Database(settings)
    try:
        async with legacy_preparation(seeded) as connection:
            await connection.run_sync(seed_predecessor)
    finally:
        await seeded.close()
    await upgrade_to_run_event_metadata(settings)
    return Database(settings)
