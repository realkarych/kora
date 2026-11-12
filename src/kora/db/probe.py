import asyncio
import enum
import importlib.util
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import NullPool, TextClause, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from kora.db.connection import (
    DATABASE_URL_VARIABLE,
    DatabaseConfigurationError,
    DatabaseSettings,
    Dialect,
    parse_database_url,
)

SQLITE_HEADER: bytes = b"SQLite format 3\x00"
SQLITE_READ_FLAGS: int = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY
)
POSTGRESQL_DRIVER_MODULE: str = "asyncpg"
REACHABILITY_QUERY: TextClause = text("SELECT 1")


class DatabaseProbeFailure(enum.StrEnum):
    NOT_CONFIGURED = "not_configured"
    INVALID_CONFIGURATION = "invalid_configuration"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    NOT_A_DATABASE = "not_a_database"
    DRIVER_MISSING = "driver_missing"
    UNREACHABLE = "unreachable"
    REJECTED = "rejected"
    DEADLINE = "deadline"


def configured_settings(
    environ: Mapping[str, str],
) -> DatabaseSettings | DatabaseProbeFailure:
    text_value = environ.get(DATABASE_URL_VARIABLE)
    if text_value is None or not text_value.strip():
        return DatabaseProbeFailure.NOT_CONFIGURED
    try:
        return parse_database_url(text_value.strip())
    except DatabaseConfigurationError:
        return DatabaseProbeFailure.INVALID_CONFIGURATION


def read_sqlite_header(path: Path) -> DatabaseProbeFailure | None:
    try:
        descriptor = os.open(path, SQLITE_READ_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return DatabaseProbeFailure.MISSING
    except OSError:
        return DatabaseProbeFailure.UNREADABLE
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return DatabaseProbeFailure.NOT_A_REGULAR_FILE
        try:
            header = os.read(descriptor, len(SQLITE_HEADER))
        except OSError:
            return DatabaseProbeFailure.UNREADABLE
    finally:
        os.close(descriptor)
    if header != SQLITE_HEADER:
        return DatabaseProbeFailure.NOT_A_DATABASE
    return None


def server_answered(error: DBAPIError) -> bool:
    sqlstate: object = getattr(error.orig, "sqlstate", None)
    return isinstance(sqlstate, str)


async def reach_postgresql(
    settings: DatabaseSettings, deadline_seconds: float
) -> DatabaseProbeFailure | None:
    if importlib.util.find_spec(POSTGRESQL_DRIVER_MODULE) is None:
        return DatabaseProbeFailure.DRIVER_MISSING
    engine = create_async_engine(settings.url, poolclass=NullPool)
    try:
        async with asyncio.timeout(deadline_seconds):
            async with engine.connect() as connection:
                await connection.execute(REACHABILITY_QUERY)
    except TimeoutError:
        return DatabaseProbeFailure.DEADLINE
    except OSError:
        return DatabaseProbeFailure.UNREACHABLE
    except DBAPIError as error:
        if server_answered(error):
            return DatabaseProbeFailure.REJECTED
        return DatabaseProbeFailure.UNREACHABLE
    finally:
        await engine.dispose()
    return None


async def probe_database(
    environ: Mapping[str, str], deadline_seconds: float
) -> Dialect | DatabaseProbeFailure:
    settings = configured_settings(environ)
    if isinstance(settings, DatabaseProbeFailure):
        return settings
    if settings.dialect is Dialect.SQLITE:
        failure = read_sqlite_header(settings.sqlite_path)
    else:
        failure = await reach_postgresql(settings, deadline_seconds)
    return settings.dialect if failure is None else failure
