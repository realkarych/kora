from collections.abc import Sequence
from typing import cast

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.run_event_page_v1 import (
    CORRUPT_EVENT_KEY,
    SQLITE_EVENT_FIELDS,
    SQLITE_EVENT_PAGE_INDEX,
    SQLITE_EVENT_SCOPE,
)
from kora.db.types import SQLITE

SQLITE_EVENT_SOURCE: str = (
    f"FROM main.event INDEXED BY {SQLITE_EVENT_PAGE_INDEX} "
    f"WHERE {SQLITE_EVENT_SCOPE} AND namespace_id = :namespace_id AND run_id = :run_id"
)
SQLITE_EVENT_KEY: str = f"({SQLITE_EVENT_FIELDS['id']})"
CORRUPT_KEY = text(
    f"SELECT EXISTS(SELECT 1 {SQLITE_EVENT_SOURCE} "
    f"AND {SQLITE_EVENT_KEY} = '{CORRUPT_EVENT_KEY}')"
)
OWNED_RESULT = """
    ((selected.node_instance_id IS NULL OR node.run_id = :run_id)
    AND (selected.attempt_id IS NULL OR (
        parent.run_id = :run_id AND (selected.node_instance_id IS NULL
        OR owned.node_instance_id = selected.node_instance_id)
    )))
    FROM selected
    LEFT JOIN {schema}node_instance AS node ON node.id = selected.node_instance_id
    LEFT JOIN {schema}attempt AS owned ON owned.id = selected.attempt_id
    LEFT JOIN {schema}node_instance AS parent ON parent.id = owned.node_instance_id
    ORDER BY selected.id DESC
    """
SQLITE_SELECTED = """
    DESC LIMIT :take
    ), selected AS (
        SELECT id, at, kind, actor,
               NULLIF(node_instance_id, '') AS node_instance_id,
               NULLIF(attempt_id, '') AS attempt_id FROM candidate
    )
    SELECT selected.id, CAST(selected.kind AS BLOB), CAST(selected.at AS BLOB),
           CAST(selected.actor AS BLOB) AS actor, selected.node_instance_id,
           selected.attempt_id,
           (SELECT CASE encoding WHEN 'UTF-8' THEN 1 WHEN 'UTF-16le' THEN 2
                       WHEN 'UTF-16be' THEN 3 ELSE 0 END FROM pragma_encoding()),
    """
POSTGRES_CANDIDATE = """
    WITH selected AS MATERIALIZED (
        SELECT id, kind, at, node_instance_id, attempt_id,
               CASE WHEN pg_catalog.pg_column_compression(actor) IS NULL
                     AND pg_catalog.pg_column_size(actor) <= 2048
                    THEN CAST(actor AS text) END AS actor
        FROM event WHERE namespace_id = :namespace_id AND run_id = :run_id
    """
POSTGRES_SELECTED = """
        ORDER BY id DESC LIMIT :take
    )
    SELECT selected.id, pg_catalog.convert_to(selected.kind, 'UTF8'), selected.at,
           CASE WHEN pg_catalog.octet_length(selected.actor) <= 1100
                THEN pg_catalog.convert_to(selected.actor, 'UTF8') END AS actor,
           selected.node_instance_id, selected.attempt_id, 1,
    """


class RunEventReadRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid retained run event")


NativeRunEvent = Row[object, object, object, object, object, object, object, object]
NativeRunEvents = Sequence[NativeRunEvent]


def _range(key: str, ceiling: str | None, after: str | None) -> str:
    upper = f" AND {key} <= :ceiling" if ceiling is not None else ""
    lower = f" AND {key} < :after" if after is not None else ""
    return upper + lower


def run_event_statement(
    sqlite: bool, ceiling: str | None, after: str | None
) -> TextClause:
    if sqlite:
        fields = ", ".join(
            f"({value}) AS {name}" for name, value in SQLITE_EVENT_FIELDS.items()
        )
        seek = _range(SQLITE_EVENT_KEY, ceiling, after)
        pieces = (
            "WITH candidate AS MATERIALIZED (SELECT",
            fields,
            SQLITE_EVENT_SOURCE,
            seek,
            "ORDER BY",
            SQLITE_EVENT_KEY,
            SQLITE_SELECTED,
        )
    else:
        pieces = (POSTGRES_CANDIDATE, _range("id", ceiling, after), POSTGRES_SELECTED)
    schema = "main." if sqlite else ""
    return text(" ".join((*pieces, OWNED_RESULT.format(schema=schema))))


async def run_event_scope(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> bool:
    prefix = "main." if connection.dialect.name == SQLITE else ""
    where = "namespace_id = :ns AND id = :run"
    params = {"ns": namespace_id, "run": run_id}
    query = text(" ".join(("SELECT 1 FROM", prefix + "run", "WHERE", where)))
    return bool(await connection.scalar(query, params))


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
