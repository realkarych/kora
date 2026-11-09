from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

WRITABLE_POSTGRES_MODE = text(
    "SELECT pg_catalog.current_setting('transaction_isolation'::pg_catalog.text) "
    "OPERATOR(pg_catalog.=) 'read committed'::pg_catalog.text AND "
    "pg_catalog.current_setting('transaction_read_only'::pg_catalog.text) "
    "OPERATOR(pg_catalog.=) 'off'::pg_catalog.text"
)


@runtime_checkable
class _NativePostgresTransaction(Protocol):
    def is_in_transaction(self) -> bool: ...


async def writable_postgres_transaction(connection: AsyncConnection) -> bool:
    if connection.dialect.driver != "asyncpg":
        return False
    try:
        raw = await connection.get_raw_connection()
        driver: object = raw.driver_connection
        if not isinstance(driver, _NativePostgresTransaction):
            return False
        if driver.is_in_transaction() is not True:
            return False
        return (await connection.scalar(WRITABLE_POSTGRES_MODE)) is True
    except SQLAlchemyError:
        return False
