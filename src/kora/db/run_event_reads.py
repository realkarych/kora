from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.identifiers import is_identifier
from kora.db.run_event_page_v1 import (
    ACTOR_BYTES,
    CORRUPT_EVENT_KEY,
    KIND_CHARACTERS,
    SQLITE_EVENT_FIELDS,
    SQLITE_EVENT_PAGE_INDEX,
    SQLITE_EVENT_SCOPE,
)
from kora.db.run_failure_summary import ENCODINGS
from kora.db.types import SQLITE, JsonObject, normalise_timestamp
from kora.ir.vcs_ref_json import decode_strict_json
from kora.observability.policy import CONTEXT_IDENTIFIER

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
           CAST(selected.actor AS BLOB), selected.node_instance_id,
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
                THEN pg_catalog.convert_to(selected.actor, 'UTF8') END,
           selected.node_instance_id, selected.attempt_id, 1,
    """
RUN_SCOPE = {
    sqlite: text(
        " ".join(
            (
                "SELECT EXISTS(SELECT 1 FROM",
                source,
                "WHERE namespace_id = :namespace_id AND id = :run_id)",
            )
        )
    )
    for sqlite, source in ((True, "main.run"), (False, "run"))
}


class RunEventReadRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid retained run event")


@dataclass(frozen=True, repr=False)
class RunEventNativeRow:
    id: object
    kind: object
    at: object
    actor: object
    node_id: object
    attempt_id: object
    encoding: object
    owned: object
    run_id: str


@dataclass(frozen=True, repr=False)
class RunEventRow:
    id: str
    kind: str
    at: datetime
    actor: JsonObject
    run_id: str
    node_instance_id: str | None
    attempt_id: str | None


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
    return bool(
        await connection.scalar(
            RUN_SCOPE[connection.dialect.name == SQLITE],
            {"namespace_id": namespace_id, "run_id": run_id},
        )
    )


async def read_run_event_page(
    connection: AsyncConnection,
    namespace_id: str,
    run_id: str,
    ceiling: str | None,
    after: str | None,
    limit: int,
) -> tuple[RunEventNativeRow, ...]:
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
    return tuple(
        RunEventNativeRow(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], run_id
        )
        for row in result
    )


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not is_identifier(value):
        raise RunEventReadRejected()
    return value


def _text(value: object, encoding: str) -> str:
    if not isinstance(value, bytes):
        raise RunEventReadRejected()
    try:
        return value.decode(encoding)
    except UnicodeError:
        raise RunEventReadRejected() from None


def _timestamp(value: object, encoding: str) -> datetime:
    try:
        if isinstance(value, datetime):
            return normalise_timestamp(value)
        raw = _text(value, encoding)
        stored = datetime.fromisoformat(raw)
        moment = normalise_timestamp(stored)
        if moment.isoformat(timespec="microseconds") != raw:
            raise RunEventReadRejected()
        return moment
    except ValueError:
        raise RunEventReadRejected() from None


def _actor(value: object, encoding: str) -> JsonObject:
    actor = decode_strict_json(_text(value, encoding).encode(), ACTOR_BYTES, 1)
    if not isinstance(actor, dict):
        raise RunEventReadRejected()
    if actor == {"kind": "system", "component": "scheduler"}:
        return actor
    if actor.keys() == {"kind", "owner", "token_id"} and actor["kind"] in (
        "user",
        "service",
    ):
        owner, token = actor["owner"], actor["token_id"]
        if all(
            isinstance(item, str) and CONTEXT_IDENTIFIER.fullmatch(item)
            for item in (owner, token)
        ):
            return actor
    raise RunEventReadRejected()


def _decode(row: RunEventNativeRow) -> RunEventRow:
    if (
        row.owned not in (True, 1)
        or type(row.encoding) is not int
        or not 1 <= row.encoding <= len(ENCODINGS)
    ):
        raise RunEventReadRejected()
    encoding = ENCODINGS[row.encoding - 1]
    kind = _text(row.kind, encoding)
    if not 1 <= len(kind) <= KIND_CHARACTERS or "\0" in kind:
        raise RunEventReadRejected()
    return RunEventRow(
        _identifier(row.id),
        kind,
        _timestamp(row.at, encoding),
        _actor(row.actor, encoding),
        row.run_id,
        None if row.node_id is None else _identifier(row.node_id),
        None if row.attempt_id is None else _identifier(row.attempt_id),
    )


def decode_run_event_page(
    rows: tuple[RunEventNativeRow, ...],
) -> tuple[RunEventRow, ...]:
    return tuple(_decode(row) for row in rows)
