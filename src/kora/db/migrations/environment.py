from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Connection, func, select
from sqlalchemy.types import TypeDecorator, TypeEngine

from kora.db.connection import MIGRATION_LEASE
from kora.db.tables import metadata
from kora.db.types import POSTGRESQL

MIGRATIONS_DIRECTORY: Path = Path(__file__).resolve().parent
HEAD: str = "head"
CONNECTION_ATTRIBUTE: str = "connection"
LOCK_TIMEOUT: str = "5s"
LOCK_TIMEOUT_SETTING: str = "lock_timeout"


@dataclass(frozen=True)
class MigrationReport:
    dialect: str
    from_revision: str | None
    to_revision: str
    steps: int


def alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
    config.set_main_option("prepend_sys_path", "")
    config.attributes[CONNECTION_ATTRIBUTE] = connection
    return config


def head_revision(config: Config) -> str:
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("the migration directory has no head revision")
    return head


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def pending_steps(config: Config, from_revision: str | None, head: str) -> int:
    script = ScriptDirectory.from_config(config)
    lower = from_revision if from_revision is not None else "base"
    return sum(1 for _ in script.iterate_revisions(head, lower))


def bound_lock_waits(connection: Connection) -> None:
    if connection.dialect.name == POSTGRESQL:
        connection.execute(
            select(func.set_config(LOCK_TIMEOUT_SETTING, LOCK_TIMEOUT, True))
        )


def upgrade_to_head(connection: Connection) -> MigrationReport:
    bound_lock_waits(connection)
    config = alembic_config(connection)
    head = head_revision(config)
    from_revision = current_revision(connection)
    steps = pending_steps(config, from_revision, head)
    if connection.info.get(MIGRATION_LEASE):
        connection.commit()
    elif connection.dialect.name == POSTGRESQL and steps:
        raise RuntimeError("PostgreSQL upgrades require a dedicated migration lease")
    command.upgrade(config, HEAD)
    return MigrationReport(
        dialect=connection.dialect.name,
        from_revision=from_revision,
        to_revision=head,
        steps=steps,
    )


def column_types_differ(
    context: MigrationContext,
    inspected_column: Column[object],
    metadata_column: Column[object],
    inspected_type: TypeEngine[object],
    metadata_type: TypeEngine[object],
) -> bool | None:
    if isinstance(metadata_type, TypeDecorator):
        effective = metadata_type.load_dialect_impl(context.dialect)
        inspected = str(inspected_type.compile(dialect=context.dialect))
        expected = str(effective.compile(dialect=context.dialect))
        return inspected.upper() != expected.upper()
    return None


def schema_differences(connection: Connection) -> Sequence[str]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": column_types_differ}
    )
    return [str(difference) for difference in compare_metadata(context, metadata)]
