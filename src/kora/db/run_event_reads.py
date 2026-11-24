from collections.abc import Sequence
from typing import cast

from sqlalchemy import (
    ColumnElement,
    Integer,
    Row,
    String,
    bindparam,
    column,
    literal_column,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.selectable import TextualSelect

from kora.db.run_event_page_v1 import (
    CORRUPT_EVENT_KEY,
    SQLITE_EVENT_FIELDS,
    SQLITE_EVENT_SCOPE,
)
from kora.db.types import SQLITE

SQLITE_SOURCE = text("main.event INDEXED BY ix_event_run_page_bounded")
POSTGRES_SOURCE = text("event")
SCOPE = (
    column("namespace_id", String()) == bindparam("namespace_id", type_=String()),
    column("run_id", String()) == bindparam("run_id", type_=String()),
)
SQLITE_FIELDS = {
    name: literal_column(f"({field})", String())
    for name, field in SQLITE_EVENT_FIELDS.items()
}
POSTGRES_FIELDS = {name: column(name, String()) for name in SQLITE_EVENT_FIELDS}
POSTGRES_FIELDS["actor"] = literal_column(
    """
    CASE WHEN pg_catalog.pg_column_compression(actor) IS NULL
          AND pg_catalog.pg_column_size(actor) <= 2048
         THEN CAST(actor AS text) END
    """,
    String(),
)
SQLITE_SCOPE: ColumnElement[bool] = literal_column(SQLITE_EVENT_SCOPE)
CORRUPT_KEY = (
    select(literal_column("1", Integer()))
    .select_from(SQLITE_SOURCE)
    .where(*SCOPE, SQLITE_SCOPE, SQLITE_FIELDS["id"] == CORRUPT_EVENT_KEY)
    .exists()
    .select()
)
SQLITE_PAGE = text("""
    SELECT selected.id, CAST(selected.kind AS BLOB), CAST(selected.at AS BLOB),
           CAST(selected.actor AS BLOB) AS actor, selected.node_instance_id,
           selected.attempt_id,
           (SELECT CASE encoding WHEN 'UTF-8' THEN 1 WHEN 'UTF-16le' THEN 2
                       WHEN 'UTF-16be' THEN 3 ELSE 0 END FROM pragma_encoding()),
           ((selected.node_instance_id IS NULL OR node.run_id = :run_id)
            AND (selected.attempt_id IS NULL OR (
                parent.run_id = :run_id AND (selected.node_instance_id IS NULL
                OR owned.node_instance_id = selected.node_instance_id)
            )))
    FROM (
        SELECT id, at, kind, actor, NULLIF(node_instance_id, '') AS node_instance_id,
               NULLIF(attempt_id, '') AS attempt_id FROM candidate
    ) AS selected
    LEFT JOIN main.node_instance AS node ON node.id = selected.node_instance_id
    LEFT JOIN main.attempt AS owned ON owned.id = selected.attempt_id
    LEFT JOIN main.node_instance AS parent ON parent.id = owned.node_instance_id
    ORDER BY selected.id DESC
    """).columns()
POSTGRES_PAGE = text("""
    SELECT selected.id, pg_catalog.convert_to(selected.kind, 'UTF8'), selected.at,
           CASE WHEN pg_catalog.octet_length(selected.actor) <= 1100
                THEN pg_catalog.convert_to(selected.actor, 'UTF8') END AS actor,
           selected.node_instance_id, selected.attempt_id, 1,
           ((selected.node_instance_id IS NULL OR node.run_id = :run_id)
            AND (selected.attempt_id IS NULL OR (
                parent.run_id = :run_id AND (selected.node_instance_id IS NULL
                OR owned.node_instance_id = selected.node_instance_id)
            )))
    FROM candidate AS selected
    LEFT JOIN node_instance AS node ON node.id = selected.node_instance_id
    LEFT JOIN attempt AS owned ON owned.id = selected.attempt_id
    LEFT JOIN node_instance AS parent ON parent.id = owned.node_instance_id
    ORDER BY selected.id DESC
    """).columns()


class RunEventReadRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid retained run event")


NativeRunEvent = Row[object, object, object, object, object, object, object, object]
NativeRunEvents = Sequence[NativeRunEvent]


def run_event_statement(
    sqlite: bool, ceiling: str | None, after: str | None
) -> TextualSelect:
    source = SQLITE_SOURCE if sqlite else POSTGRES_SOURCE
    fields = SQLITE_FIELDS if sqlite else POSTGRES_FIELDS
    key = fields["id"]
    projected = (field.label(name) for name, field in fields.items())
    scope = (*SCOPE, SQLITE_SCOPE) if sqlite else SCOPE
    query = select(*projected).select_from(source).where(*scope)
    if ceiling is not None:
        query = query.where(key <= bindparam("ceiling", type_=String()))
    if after is not None:
        query = query.where(key < bindparam("after", type_=String()))
    candidate = query.order_by(key.desc()).limit(bindparam("take", type_=Integer()))
    projection = SQLITE_PAGE if sqlite else POSTGRES_PAGE
    return projection.add_cte(candidate.cte("candidate").prefix_with("MATERIALIZED"))


async def run_event_scope(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> bool:
    statement = (
        text("SELECT 1 FROM main.run WHERE namespace_id = :ns AND id = :run")
        if connection.dialect.name == SQLITE
        else text("SELECT 1 FROM run WHERE namespace_id = :ns AND id = :run")
    )
    return bool(await connection.scalar(statement, {"ns": namespace_id, "run": run_id}))


async def read_run_event_page(
    connection: AsyncConnection,
    namespace_id: str,
    run_id: str,
    ceiling: str | None,
    after: str | None,
    limit: int,
) -> NativeRunEvents:
    if not 1 <= limit <= 100:
        raise ValueError("invalid run event page limit")
    parameters: dict[str, str | int | None] = {
        "namespace_id": namespace_id,
        "run_id": run_id,
        "ceiling": ceiling,
        "after": after,
        "take": limit + 1,
    }
    sqlite = connection.dialect.name == SQLITE
    if sqlite and await connection.scalar(CORRUPT_KEY, parameters):
        raise RunEventReadRejected()
    result = await connection.execute(
        run_event_statement(sqlite, ceiling, after), parameters
    )
    return cast("NativeRunEvents", result.all())
