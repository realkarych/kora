from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Connection
from sqlalchemy.schema import SchemaItem
from sqlalchemy.types import TypeDecorator, TypeEngine

from kora.db.attempt_capacity_v1 import SQLITE_DEPLOYED_INDEX
from kora.db.claim_sources_v1 import LOCATORS
from kora.db.connection import (
    MIGRATION_LEASE,
    MIGRATION_LOCK_TIMEOUT,
    POSTGRESQL_MIGRATION_TIMEOUT_QUERY,
)
from kora.db.graph_publication_digest_v1 import INVALID_ID_INDEX
from kora.db.graph_publication_ordinal_v1 import (
    ORDINAL_INVALID_INDEX,
    ORDINAL_VALID_INDEX,
)
from kora.db.run_input_locator_v1 import (
    SQLITE_RUN_INPUT_LOCATOR_INDEX,
    SQLITE_RUN_LOCATOR_INDEX,
)
from kora.db.run_queue_index_v1 import POSTGRES_QUEUE_INDEX, SQLITE_QUEUE_INDEX
from kora.db.tables import GRAPH_VERSION_SCOPE_INDEX, metadata
from kora.db.types import POSTGRESQL, SQLITE

MIGRATIONS_DIRECTORY: Path = Path(__file__).resolve().parent
HEAD: str = "head"
CONNECTION_ATTRIBUTE: str = "connection"


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
            POSTGRESQL_MIGRATION_TIMEOUT_QUERY,
            {"value": MIGRATION_LOCK_TIMEOUT, "local": True},
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


def include_schema_object(
    dialect: str,
    _item: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    if (
        type_ == "index"
        and not reflected
        and compare_to is None
        and (
            (name == POSTGRES_QUEUE_INDEX and dialect == SQLITE)
            or (name == SQLITE_QUEUE_INDEX and dialect == POSTGRESQL)
        )
    ):
        return False
    return not (
        dialect == POSTGRESQL
        and type_ == "index"
        and name
        in (
            GRAPH_VERSION_SCOPE_INDEX,
            INVALID_ID_INDEX,
            ORDINAL_VALID_INDEX,
            ORDINAL_INVALID_INDEX,
            SQLITE_RUN_LOCATOR_INDEX,
            SQLITE_RUN_INPUT_LOCATOR_INDEX,
            SQLITE_DEPLOYED_INDEX,
            *(index for _, _, index in LOCATORS),
        )
        and not reflected
        and compare_to is None
    )


def schema_differences(connection: Connection) -> Sequence[str]:
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": column_types_differ,
            "include_object": partial(include_schema_object, connection.dialect.name),
        },
    )
    return [str(difference) for difference in compare_metadata(context, metadata)]
