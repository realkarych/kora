import sqlite3
from collections.abc import Callable
from typing import Protocol, cast, runtime_checkable

from aiosqlite import Connection as SQLiteDriver
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.graph_deployment_stage_reads import PAYLOAD_DIGEST_WIDTH, STORE_ID_WIDTH
from kora.db.graph_stage_promotion_custody_v1 import INTENT_TABLE

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
