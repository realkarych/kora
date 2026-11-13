from functools import partial
from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection

from kora.db.migrations.environment import (
    CONNECTION_ATTRIBUTE,
    column_types_differ,
    include_schema_object,
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
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
