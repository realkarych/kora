import asyncio
import enum
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from kora.db.connection import (
    POSTGRESQL_DRIVER,
    SQLITE_DRIVER,
    DatabaseSettings,
    Dialect,
    parse_database_url,
)
from kora.db.migrations import upgrade_database

DATABASES_VARIABLE: str = "KORA_TEST_DATABASES"
RYUK_VARIABLE: str = "TESTCONTAINERS_RYUK_DISABLED"
POSTGRES_TAG: str = "postgres:16.15"
POSTGRES_DIGEST: str = (
    "sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d"
)
POSTGRES_IMAGE: str = f"{POSTGRES_TAG}@{POSTGRES_DIGEST}"
POSTGRES_PORT: int = 5432
POSTGRES_USER: str = "kora"
TEMPLATE_DATABASE: str = "kora_template"
ADMIN_DATABASE: str = "postgres"
EMPTY_TEMPLATE: str = "template1"
READINESS_DEADLINE_SECONDS: float = 60.0
READINESS_INTERVAL_SECONDS: float = 0.1
DATABASE_NAME_PATTERN: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_]{0,62}")
SQLITE_TEMPLATE_NAME: str = "template.db"


class DatabaseKind(enum.StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


def selected_databases(environ: Mapping[str, str]) -> tuple[DatabaseKind, ...]:
    raw = environ.get(DATABASES_VARIABLE)
    if raw is None:
        return (DatabaseKind.SQLITE, DatabaseKind.POSTGRESQL)
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise pytest.UsageError(f"{DATABASES_VARIABLE} names at least one database")
    kinds: list[DatabaseKind] = []
    for name in names:
        try:
            kind = DatabaseKind(name)
        except ValueError:
            raise pytest.UsageError(
                f"{DATABASES_VARIABLE} accepts sqlite and postgresql, not {name!r}"
            ) from None
        if kind not in kinds:
            kinds.append(kind)
    return tuple(kinds)


def wait_until(condition: Callable[[], bool], deadline_seconds: float) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("the condition was not met before the deadline")
        time.sleep(READINESS_INTERVAL_SECONDS)


def sqlite_settings(path: Path) -> DatabaseSettings:
    return parse_database_url(f"{SQLITE_DRIVER}:///{path}")


def valid_database_name(name: str) -> str:
    if DATABASE_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("a test database name is a short lower-case identifier")
    return name


@dataclass(frozen=True)
class PostgresServer:
    host: str
    port: int
    password: str

    def url_for(self, database: str) -> URL:
        return URL.create(
            POSTGRESQL_DRIVER,
            username=POSTGRES_USER,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database,
        )

    def settings_for(self, database: str) -> DatabaseSettings:
        return DatabaseSettings(url=self.url_for(database), dialect=Dialect.POSTGRESQL)

    def admin_url(self) -> URL:
        return self.url_for(ADMIN_DATABASE)

    async def _execute_admin(self, statement: str) -> None:
        engine = create_async_engine(self.admin_url(), isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    async def _ready(self) -> bool:
        engine = create_async_engine(self.admin_url())
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        finally:
            await engine.dispose()
        return True

    def wait_ready(self) -> None:
        wait_until(lambda: asyncio.run(self._ready()), READINESS_DEADLINE_SECONDS)

    async def create_database_in_loop(self, name: str, template: str) -> None:
        quoted_name = quote(valid_database_name(name))
        quoted_template = quote(valid_database_name(template))
        await self._execute_admin(
            f"CREATE DATABASE {quoted_name} TEMPLATE {quoted_template}"
        )

    async def drop_database_in_loop(self, name: str) -> None:
        quoted_name = quote(valid_database_name(name))
        await self._execute_admin(f"DROP DATABASE {quoted_name} WITH (FORCE)")

    def create_database(self, name: str, template: str) -> None:
        asyncio.run(self.create_database_in_loop(name, template))

    def drop_database(self, name: str) -> None:
        asyncio.run(self.drop_database_in_loop(name))

    def connection_count(self) -> int:
        return asyncio.run(self._count_connections())

    async def _count_connections(self) -> int:
        engine = create_async_engine(self.admin_url())
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname IS NOT NULL AND pid <> pg_backend_pid()"
                    )
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def start_postgres(environ: Mapping[str, str]) -> Iterator[PostgresServer]:
    if environ.get(RYUK_VARIABLE) is None:
        os.environ[RYUK_VARIABLE] = "true"
    password = secrets.token_urlsafe(24)
    container = PostgresContainer(
        image=POSTGRES_IMAGE,
        port=POSTGRES_PORT,
        username=POSTGRES_USER,
        password=password,
        dbname=TEMPLATE_DATABASE,
        driver="asyncpg",
    )
    container.start()
    try:
        server = PostgresServer(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(POSTGRES_PORT),
            password=password,
        )
        server.wait_ready()
        asyncio.run(upgrade_database(server.settings_for(TEMPLATE_DATABASE)))
        yield server
    finally:
        container.stop()


def prepare_sqlite_template(directory: Path) -> Path:
    template = directory / SQLITE_TEMPLATE_NAME
    asyncio.run(upgrade_database(sqlite_settings(template)))
    return template


def copy_sqlite_template(template: Path, destination: Path) -> DatabaseSettings:
    shutil.copyfile(template, destination)
    destination.chmod(0o600)
    return sqlite_settings(destination)
