from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection

from kora.db.migrations.environment import CONNECTION_ATTRIBUTE, column_types_differ
from kora.db.tables import metadata
from kora.db.types import CollatedText, JsonDocument, UsdAmount, UtcTimestamp

TYPES_MODULE: str = "kora.db.types"
RENDERED_TYPES: tuple[
    type[CollatedText], type[JsonDocument], type[UsdAmount], type[UtcTimestamp]
] = (
    CollatedText,
    JsonDocument,
    UsdAmount,
    UtcTimestamp,
)


def render_item(
    type_: str, obj: object, autogen_context: AutogenContext
) -> str | Literal[False]:
    if type_ != "type" or not isinstance(obj, RENDERED_TYPES):
        return False
    autogen_context.imports.add(f"import {TYPES_MODULE}")
    if isinstance(obj, CollatedText):
        return f"{TYPES_MODULE}.CollatedText({obj.length})"
    return f"{TYPES_MODULE}.{type(obj).__name__}()"


def run_migrations() -> None:
    connection: Connection = context.config.attributes[CONNECTION_ATTRIBUTE]
    context.configure(
        connection=connection,
        target_metadata=metadata,
        compare_type=column_types_differ,
        render_item=render_item,
        transaction_per_migration=connection.dialect.name == "postgresql",
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
