import sqlite3
from typing import Final

from sqlalchemy import Integer, literal_column, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as DatabaseTimeout
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseClosedError, Dialect
from kora.db.identifiers import is_identifier
from kora.db.tables import api_token
from kora.db.token_statements import REVOKED as REVOKED
from kora.db.token_statements import revoke_token_statement
from kora.db.transactions import raise_private_metadata_error, writer_transaction
from kora.db.types import POSTGRESQL, SQLITE

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
    if database.dialect is Dialect.SQLITE:
        return (*errors, sqlite3.Error)
    native_error = database.postgresql_driver_error()
    return errors if native_error is None else (*errors, native_error)


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
        revoke_token_statement(), {"token_id": token_id, "revoked": REVOKED}
    )
