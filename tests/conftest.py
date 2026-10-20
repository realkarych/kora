import itertools
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from kora.db.connection import Database, DatabaseSettings
from tests.support.databases import (
    EMPTY_TEMPLATE,
    TEMPLATE_DATABASE,
    DatabaseKind,
    PostgresServer,
    copy_sqlite_template,
    prepare_sqlite_template,
    selected_databases,
    sqlite_settings,
    start_postgres,
)

SELECTED_DATABASES: tuple[DatabaseKind, ...] = selected_databases(os.environ)
DATABASE_KIND_FIXTURE: str = "database_kind"
DATABASE_NUMBERS: Iterator[int] = itertools.count(1)


def pytest_report_header(config: pytest.Config) -> list[str]:
    names = ", ".join(kind.value for kind in SELECTED_DATABASES)
    return [f"kora test databases: {names}"]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if DATABASE_KIND_FIXTURE in metafunc.fixturenames:
        metafunc.parametrize(
            DATABASE_KIND_FIXTURE,
            list(SELECTED_DATABASES),
            ids=[kind.value for kind in SELECTED_DATABASES],
        )


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[PostgresServer]:
    yield from start_postgres(os.environ)


@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return prepare_sqlite_template(tmp_path_factory.mktemp("sqlite-template"))


@pytest.fixture
def database_settings(
    database_kind: DatabaseKind,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    sqlite_template: Path,
) -> Iterator[DatabaseSettings]:
    if database_kind is DatabaseKind.SQLITE:
        yield copy_sqlite_template(sqlite_template, tmp_path / "kora.db")
        return
    server: PostgresServer = request.getfixturevalue("postgres_server")
    name = f"kora_test_{os.getpid()}_{next(DATABASE_NUMBERS)}"
    server.create_database(name, TEMPLATE_DATABASE)
    try:
        yield server.settings_for(name)
    finally:
        server.drop_database(name)


@pytest.fixture
def empty_database_settings(
    database_kind: DatabaseKind, request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[DatabaseSettings]:
    if database_kind is DatabaseKind.SQLITE:
        yield sqlite_settings(tmp_path / "empty.db")
        return
    server: PostgresServer = request.getfixturevalue("postgres_server")
    name = f"kora_empty_{os.getpid()}_{next(DATABASE_NUMBERS)}"
    server.create_database(name, EMPTY_TEMPLATE)
    try:
        yield server.settings_for(name)
    finally:
        server.drop_database(name)


@pytest_asyncio.fixture
async def database(database_settings: DatabaseSettings) -> AsyncIterator[Database]:
    created = Database(database_settings)
    try:
        yield created
    finally:
        await created.close()
