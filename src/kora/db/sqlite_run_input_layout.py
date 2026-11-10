from asyncio import CancelledError

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.connection import clear_snapshot_error, complete
from kora.db.maintenance import active_sqlite_driver
from kora.db.run_input_locator_v1 import (
    SQLITE_RUN_INPUT_KEY,
    SQLITE_RUN_INPUT_LOCATOR_INDEX,
    SQLITE_RUN_KEY,
    SQLITE_RUN_LOCATOR_INDEX,
)
from kora.db.types import SQLITE

_QUERY_ONLY = text("PRAGMA query_only")
_ENCODING = text("PRAGMA main.encoding")
_RELATIONS = text("""
WITH expected(name, kind, owner, definition) AS (
    VALUES ('run', 'table', 'run', NULL),
           ('run_inputs', 'table', 'run_inputs', NULL),
           (:run_index, 'index', 'run', :run_definition),
           (:input_index, 'index', 'run_inputs', :input_definition)
)
SELECT count(*) = 4 AND count(DISTINCT native.rootpage) = 4
FROM expected
JOIN main.sqlite_schema AS native
  ON native.name = expected.name COLLATE BINARY
 AND native.type = expected.kind COLLATE BINARY
 AND native.tbl_name = expected.owner COLLATE BINARY
 AND native.rootpage > 0
 AND (expected.definition IS NULL OR native.sql = expected.definition COLLATE BINARY)
""")
_RELATION_PARAMETERS: dict[str, str] = {
    "run_index": SQLITE_RUN_LOCATOR_INDEX,
    "run_definition": (
        f"CREATE INDEX {SQLITE_RUN_LOCATOR_INDEX} ON run (id COLLATE BINARY) "
        f"WHERE {SQLITE_RUN_KEY}"
    ),
    "input_index": SQLITE_RUN_INPUT_LOCATOR_INDEX,
    "input_definition": (
        f"CREATE INDEX {SQLITE_RUN_INPUT_LOCATOR_INDEX} "
        f"ON run_inputs (run_id COLLATE BINARY) WHERE {SQLITE_RUN_INPUT_KEY}"
    ),
}

CatalogRows = tuple[tuple[object, ...], ...]


def _columns(specification: tuple[tuple[str, str, int], ...]) -> CatalogRows:
    return tuple(
        (position, name, kind, required, None, int(position == 0), 0)
        for position, (name, kind, required) in enumerate(specification)
    )


_RUN_COLUMNS = _columns(
    (
        ("id", "VARCHAR(26)", 1),
        ("namespace_id", "VARCHAR(26)", 1),
        ("graph_id", "VARCHAR(26)", 1),
        ("graph_version_id", "VARCHAR(26)", 1),
        ("rerun_of_run_id", "VARCHAR(26)", 0),
        ("idempotency_key", "VARCHAR(255)", 1),
        ("target_key", "VARCHAR(255)", 0),
        ("target_held_since", "VARCHAR(32)", 0),
        ("params", "TEXT", 1),
        ("cel_params_projection", "TEXT", 1),
        ("allow_unenforced_secrets", "BOOLEAN", 1),
        ("status", "VARCHAR(255)", 1),
        ("needs_scheduling", "BOOLEAN", 1),
        ("trigger", "TEXT", 1),
        ("limits", "TEXT", 1),
        ("retention", "TEXT", 1),
        ("started_at", "VARCHAR(32)", 0),
        ("deadline_expired_at", "VARCHAR(32)", 0),
        ("finished_at", "VARCHAR(32)", 0),
        ("created_at", "VARCHAR(32)", 1),
        ("updated_at", "VARCHAR(32)", 1),
        ("retention_version", "INTEGER", 0),
        ("input_encoding_version", "INTEGER", 0),
    )
)
_INPUT_COLUMNS = _columns(
    (
        ("run_id", "VARCHAR(26)", 1),
        ("cel_projection", "BLOB", 1),
        ("params", "BLOB", 1),
    )
)
_ROWID_SUFFIX = (1, -1, None, 0, "BINARY", 0)
_SHAPES: tuple[tuple[TextClause, CatalogRows], ...] = (
    (text("PRAGMA main.table_xinfo(run)"), _RUN_COLUMNS),
    (text("PRAGMA main.table_xinfo(run_inputs)"), _INPUT_COLUMNS),
    (
        text("PRAGMA main.index_xinfo(ix_run_input_locator)"),
        ((0, 0, "id", 0, "BINARY", 1), _ROWID_SUFFIX),
    ),
    (
        text("PRAGMA main.index_xinfo(ix_run_inputs_locator)"),
        ((0, 0, "run_id", 0, "BINARY", 1), _ROWID_SUFFIX),
    ),
)


async def _catalog_rows(
    connection: AsyncConnection,
    statement: TextClause,
    count: int,
    parameters: dict[str, str] | None = None,
) -> CatalogRows:
    result = await connection.stream(statement, parameters)
    try:
        return tuple(tuple(row) for row in await result.fetchmany(count + 1))
    finally:
        await result.close()


async def observe_sqlite_run_input_layout(connection: AsyncConnection) -> bool:
    root = None
    original = None
    try:
        root = connection.get_transaction()
        if (
            connection.dialect.name != SQLITE
            or root is None
            or not root.is_active
            or connection.in_nested_transaction()
        ):
            return False
        original = await connection.run_sync(active_sqlite_driver)
        if original is None:
            return False
        if await complete(_catalog_rows(connection, _QUERY_ONLY, 1)) != ((1,),):
            return False
        encoding = await complete(_catalog_rows(connection, _ENCODING, 1))
        if encoding not in ((("UTF-8",),), (("UTF-16le",),), (("UTF-16be",),)):
            return False
        relations = await complete(
            _catalog_rows(connection, _RELATIONS, 1, _RELATION_PARAMETERS)
        )
        if relations != ((1,),):
            return False
        for statement, expected in _SHAPES:
            if (
                await complete(_catalog_rows(connection, statement, len(expected)))
                != expected
            ):
                return False
        return (
            connection.get_transaction() is root
            and root.is_active
            and not connection.in_nested_transaction()
            and await connection.run_sync(active_sqlite_driver) is original
        )
    except (OSError, SQLAlchemyError):
        return False
    except CancelledError as error:
        clear_snapshot_error(error)
        del connection, root, original
        raise
