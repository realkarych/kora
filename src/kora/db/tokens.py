import sqlite3
from datetime import UTC, datetime
from typing import Final, Protocol, cast

from sqlalchemy import Integer, literal_column, or_, select, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as DatabaseTimeout
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseClosedError, Dialect
from kora.db.identifiers import is_identifier
from kora.db.tables import api_token
from kora.db.transactions import raise_private_metadata_error, writer_transaction
from kora.db.types import POSTGRESQL, SQLITE

REVOKED: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
UNAVAILABLE_SQLITE: Final[frozenset[int]] = frozenset(
    (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_PROTOCOL,
    )
)


class PostgresDriver(Protocol):
    PostgresError: type[Exception]


class PostgresAdapter(Protocol):
    asyncpg: PostgresDriver


class GraphCredentialGuardRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph credential guard rejected")


class GraphCredentialGuardFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph credential guard failed")


async def acquire_graph_credential_guard(
    connection: AsyncConnection, token_id: str
) -> None:
    lease = writer_transaction(connection)
    dialect = connection.dialect.name
    if (
        dialect not in (POSTGRESQL, SQLITE)
        or type(token_id) is not str
        or not is_identifier(token_id)
    ):
        raise_private_metadata_error(GraphCredentialGuardRejected())
    statement = select(literal_column("1", Integer)).where(api_token.c.id == token_id)
    if dialect == POSTGRESQL:
        statement = statement.with_for_update(read=True, of=api_token)
    try:
        marker: object = (await connection.execute(statement)).scalar_one_or_none()
    except SQLAlchemyError:
        raise_private_metadata_error(GraphCredentialGuardFailed())
    lease.require_active()
    if marker != 1:
        raise_private_metadata_error(GraphCredentialGuardRejected())


def authentication_errors(database: Database) -> tuple[type[Exception], ...]:
    errors: tuple[type[Exception], ...] = (
        SQLAlchemyError,
        OSError,
        DatabaseClosedError,
        ValueError,
    )
    if database.dialect is Dialect.POSTGRESQL:
        adapter = cast("PostgresAdapter", database.writer_engine.dialect.loaded_dbapi)
        return (*errors, adapter.asyncpg.PostgresError)
    return errors


def authentication_unavailable(error: Exception) -> bool:
    if isinstance(error, OSError | DatabaseClosedError | DatabaseTimeout):
        return True
    if isinstance(error, DBAPIError):
        if error.connection_invalidated:
            return True
        original = error.orig
    else:
        original = error
    sqlite_code: object = getattr(original, "sqlite_errorcode", None)
    sqlstate: object = getattr(original, "sqlstate", None)
    return (
        isinstance(sqlite_code, int) and sqlite_code & 255 in UNAVAILABLE_SQLITE
    ) or (
        isinstance(sqlstate, str)
        and (
            sqlstate.startswith(("08", "53")) or sqlstate in {"57P01", "57P02", "57P03"}
        )
    )


async def revoke_token(connection: AsyncConnection, token_id: str) -> None:
    await connection.execute(
        update(api_token)
        .where(
            api_token.c.id == token_id,
            or_(api_token.c.expires_at.is_(None), api_token.c.expires_at != REVOKED),
        )
        .values(expires_at=REVOKED)
    )
