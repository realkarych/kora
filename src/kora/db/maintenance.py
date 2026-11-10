import sqlite3
from asyncio import CancelledError
from collections.abc import Callable
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from aiosqlite import Connection as SQLiteDriver
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.graph_deployment_stage_reads import PAYLOAD_DIGEST_WIDTH, STORE_ID_WIDTH
from kora.db.graph_stage_promotion_custody_v1 import INTENT_TABLE
from kora.db.transactions import WriterTransactionError, writer_transaction
from kora.db.types import SQLITE

_SQLITE_INTENT_METADATA: str = """
SELECT rowid, ?, typeof(store_id), typeof(payload_digest),
       typeof(operation_id), typeof(request_digest),
       typeof(fence_incarnation), typeof(content_generation)
  FROM main.graph_deployment_stage_promotion_intent
       INDEXED BY sqlite_autoindex_graph_deployment_stage_promotion_intent_1
 WHERE stage_id COLLATE BINARY = ?
"""
PROMOTION_INTENT_TEXT_WIDTHS: tuple[tuple[str, int, int], ...] = (
    ("store_id", 1, STORE_ID_WIDTH),
    ("payload_digest", PAYLOAD_DIGEST_WIDTH, PAYLOAD_DIGEST_WIDTH),
    ("operation_id", 1, STORE_ID_WIDTH),
    ("request_digest", PAYLOAD_DIGEST_WIDTH, PAYLOAD_DIGEST_WIDTH),
)
_SQLITE_INTENT_STORAGE: tuple[str, ...] = (
    "text",
    "text",
    "text",
    "text",
    "integer",
    "integer",
)

PUBLICATION_TRIGGER_CHECK: TextClause = text("""
WITH targets(name) AS (
    VALUES ('graph'), ('graph_version'), ('graph_version_read_admission'),
           ('graph_version_resource_owner'), ('graph_deployment_preparation'),
           ('idempotency_record'), ('event')
)
SELECT NOT EXISTS (
    SELECT 1 FROM main.sqlite_schema
    WHERE type = 'trigger' AND tbl_name COLLATE NOCASE IN (SELECT name FROM targets)
    UNION ALL
    SELECT 1 FROM temp.sqlite_schema
    WHERE type = 'trigger' AND tbl_name COLLATE NOCASE IN (SELECT name FROM targets)
)
""")
PUBLICATION_FOREIGN_KEY_CHECK: TextClause = text("""
WITH targets(name) AS (
    VALUES ('graph'), ('graph_version'), ('graph_version_read_admission'),
           ('graph_version_resource_owner'), ('graph_deployment_preparation'),
           ('idempotency_record'), ('event')
)
SELECT CASE
    WHEN EXISTS (
        SELECT 1 FROM main.sqlite_schema
        WHERE name COLLATE NOCASE IN ('pragma_foreign_keys', 'pragma_foreign_key_list')
    ) THEN 0
    WHEN EXISTS (
        SELECT 1 FROM main.pragma_foreign_keys() AS enforcement
        WHERE typeof(enforcement.foreign_keys) = 'integer'
          AND enforcement.foreign_keys = 1
    ) THEN NOT EXISTS (
        SELECT 1 FROM main.sqlite_schema AS child
        CROSS JOIN main.pragma_foreign_key_list(child.name, 'main') AS native
        WHERE child.type = 'table'
          AND (child.name COLLATE NOCASE IN (SELECT name FROM targets)
               OR native."table" COLLATE NOCASE IN (SELECT name FROM targets))
          AND (native.on_update IS NOT 'NO ACTION'
               OR native.on_delete IS NOT 'NO ACTION')
    )
    ELSE 0
END
""")


class PromotionIntentScalarsRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph stage promotion intent scalars rejected")


def require_promotion_intent_width(
    width: object, lower: int, upper: int, factor: int
) -> None:
    if (
        type(width) is not int
        or not lower * factor <= width <= upper * factor
        or width % factor
    ):
        raise PromotionIntentScalarsRejected()


class _SQLiteIntentWidthRow:
    __slots__ = ()

    def __init__(self, cursor: sqlite3.Cursor, row: tuple[object, ...]) -> None:
        if len(row) != 2 + len(_SQLITE_INTENT_STORAGE):
            raise PromotionIntentScalarsRejected()
        rowid, factor, *storage = row
        if (
            type(rowid) is not int
            or type(factor) is not int
            or factor not in (1, 2)
            or tuple(storage) != _SQLITE_INTENT_STORAGE
        ):
            raise PromotionIntentScalarsRejected()
        for name, lower, upper in PROMOTION_INTENT_TEXT_WIDTHS:
            with cursor.connection.blobopen(
                INTENT_TABLE, name, rowid, readonly=True, name="main"
            ) as value:
                require_promotion_intent_width(len(value), lower, upper, factor)


async def observe_sqlite_promotion_intent(
    connection: AsyncConnection,
    stage_id: str,
    width_factor: int,
    check_deadline: Callable[[], None],
) -> bool:
    check_deadline()
    if connection.invalidated:
        raise PromotionIntentScalarsRejected()
    raw = await connection.get_raw_connection()
    driver: object = raw.driver_connection
    if not isinstance(driver, SQLiteDriver) or not driver.in_transaction:
        raise PromotionIntentScalarsRejected()
    async with driver.cursor() as cursor:
        cursor.row_factory = _SQLiteIntentWidthRow
        check_deadline()
        await cursor.execute(_SQLITE_INTENT_METADATA, (width_factor, stage_id))
        check_deadline()
        value = cast(object, await cursor.fetchone())
    if value is None:
        return False
    if type(value) is not _SQLiteIntentWidthRow:
        raise PromotionIntentScalarsRejected()
    return True


class PostgreSQLDurabilityError(RuntimeError):
    pass


@runtime_checkable
class _PostgreSQLTransactionState(Protocol):
    def is_in_transaction(self) -> bool: ...


def postgresql_transaction_active(connection: Connection) -> bool:
    if connection.closed or connection.invalidated:
        raise PostgreSQLDurabilityError("PostgreSQL migration backend is unavailable")
    driver: object = connection.connection.driver_connection
    if not isinstance(driver, _PostgreSQLTransactionState):
        raise PostgreSQLDurabilityError("PostgreSQL migration backend is unavailable")
    active = driver.is_in_transaction()
    if type(active) is not bool:
        raise PostgreSQLDurabilityError("PostgreSQL migration backend is unavailable")
    return active


class SQLitePublicationTriggerObservation(Enum):
    VERIFIED = "verified"
    REFUSED = "refused"


class SQLitePublicationForeignKeyObservation(Enum):
    VERIFIED = "verified"
    REFUSED = "refused"


def active_sqlite_driver(connection: Connection) -> SQLiteDriver | None:
    if connection.closed or connection.invalidated:
        return None
    driver: object = connection.connection.driver_connection
    if not isinstance(driver, SQLiteDriver):
        return None
    try:
        return driver if driver.in_transaction else None
    except ValueError:
        return None


async def _verify_sqlite_publication_catalog(
    connection: AsyncConnection, statement: TextClause
) -> bool:
    if connection.dialect.name != SQLITE:
        return False
    try:
        writer = writer_transaction(connection)
        original = await connection.run_sync(active_sqlite_driver)
        if original is None:
            return False
        verified: object = await connection.scalar(statement)
        writer.require_active()
        if (
            type(verified) is int
            and verified == 1
            and await connection.run_sync(active_sqlite_driver) is original
        ):
            return True
    except (WriterTransactionError, SQLAlchemyError):
        return False
    except CancelledError as error:
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        raise
    return False


async def observe_sqlite_publication_triggers(
    connection: AsyncConnection,
) -> SQLitePublicationTriggerObservation:
    verified = await _verify_sqlite_publication_catalog(
        connection, PUBLICATION_TRIGGER_CHECK
    )
    return (
        SQLitePublicationTriggerObservation.VERIFIED
        if verified
        else SQLitePublicationTriggerObservation.REFUSED
    )


async def observe_sqlite_publication_foreign_keys(
    connection: AsyncConnection,
) -> SQLitePublicationForeignKeyObservation:
    verified = await _verify_sqlite_publication_catalog(
        connection, PUBLICATION_FOREIGN_KEY_CHECK
    )
    return (
        SQLitePublicationForeignKeyObservation.VERIFIED
        if verified
        else SQLitePublicationForeignKeyObservation.REFUSED
    )
