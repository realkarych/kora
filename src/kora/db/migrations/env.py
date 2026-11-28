from functools import partial
from typing import Literal, cast

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection

from kora.db.migrations.environment import (
    CONNECTION_ATTRIBUTE,
    column_types_differ,
    include_schema_object,
)
from kora.db.run_event_maintenance_admission import (
    PositionMaintenanceRefused,
    earlier_position_target,
    position_migration_authority,
)
from kora.db.tables import metadata
from kora.db.types import (
    CollatedText,
    JsonDocument,
    OutcomePayloadText,
    UsdAmount,
    UtcTimestamp,
)
from kora.db.usage_source import UsageSourceText

TYPES_MODULE: str = "kora.db.types"
RENDERED_TYPES: tuple[
    type[CollatedText],
    type[JsonDocument],
    type[OutcomePayloadText],
    type[UsageSourceText],
    type[UsdAmount],
    type[UtcTimestamp],
] = (
    CollatedText,
    JsonDocument,
    OutcomePayloadText,
    UsageSourceText,
    UsdAmount,
    UtcTimestamp,
)


def render_item(
    type_: str, obj: object, autogen_context: AutogenContext
) -> str | Literal[False]:
    if type_ != "type" or not isinstance(obj, RENDERED_TYPES):
        return False
    module = (
        "kora.db.usage_source" if isinstance(obj, UsageSourceText) else TYPES_MODULE
    )
    autogen_context.imports.add(f"import {module}")
    if isinstance(obj, CollatedText):
        length = (
            f", postgresql_length={obj.postgresql_length}"
            if obj.postgresql_length is not None
            else ""
        )
        return f"{TYPES_MODULE}.CollatedText({obj.length}{length})"
    return f"{module}.{type(obj).__name__}()"


def run_migrations() -> None:
    connection: Connection = context.config.attributes[CONNECTION_ATTRIBUTE]
    context.configure(
        connection=connection,
        target_metadata=metadata,
        compare_type=column_types_differ,
        include_object=partial(include_schema_object, connection.dialect.name),
        render_item=render_item,
        transaction_per_migration=connection.dialect.name == "postgresql",
    )
    destination: object = context.get_context().opts.get("destination_rev")
    if destination is not None and not earlier_target(destination):
        if context.is_offline_mode() or isinstance(destination, tuple):
            raise PositionMaintenanceRefused
        position_migration_authority(connection)
    with context.begin_transaction():
        context.run_migrations()


def earlier_target(value: object) -> bool:
    if isinstance(value, tuple):
        return all(earlier_target(target) for target in cast(tuple[object, ...], value))
    return isinstance(value, str) and earlier_position_target(value)


run_migrations()
