from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy import (
    event,
)

from kora.db.connection import (
    Database,
    DatabaseSettings,
)
from tests.measurements.harness import (
    Report,
    Sample,
)
from tests.support.databases import (
    EMPTY_TEMPLATE,
    TEMPLATE_DATABASE,
    DatabaseKind,
    PostgresServer,
    copy_sqlite_template,
    prepare_sqlite_template,
    sqlite_settings,
)

SLOW_SQLITE_STATEMENT: str = (
    "WITH RECURSIVE counter(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM counter "
    "WHERE n < 200000) SELECT count(*) FROM counter"
)
SLOW_POSTGRESQL_STATEMENT: str = "SELECT pg_sleep(0.005)"


class DatabaseFactory:
    def __init__(
        self, kind: DatabaseKind, directory: Path, server: PostgresServer | None
    ) -> None:
        self.kind = kind
        self.directory = directory
        self.server = server
        self.created: list[str] = []
        self.sqlite_template = (
            prepare_sqlite_template(directory) if server is None else None
        )
        self._counter = 0

    def _next_name(self) -> str:
        self._counter += 1
        return f"kora_measure_{os.getpid()}_{self._counter}"

    async def empty(self) -> DatabaseSettings:
        if self.server is None:
            return sqlite_settings(self.directory / f"{self._next_name()}.db")
        name = self._next_name()
        await self.server.create_database_in_loop(name, EMPTY_TEMPLATE)
        self.created.append(name)
        return self.server.settings_for(name)

    async def migrated(self) -> DatabaseSettings:
        if self.server is None:
            assert self.sqlite_template is not None
            return copy_sqlite_template(
                self.sqlite_template, self.directory / f"{self._next_name()}.db"
            )
        name = self._next_name()
        await self.server.create_database_in_loop(name, TEMPLATE_DATABASE)
        self.created.append(name)
        return self.server.settings_for(name)

    async def discard(self, settings: DatabaseSettings) -> None:
        if self.server is None:
            path = settings.sqlite_path
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                if candidate.exists():
                    candidate.unlink()
            return
        name = str(settings.url.database)
        if name in self.created:
            await self.server.drop_database_in_loop(name)
            self.created.remove(name)

    def temporary_files(self, settings: DatabaseSettings) -> list[str]:
        if self.server is not None:
            return []
        path = settings.sqlite_path
        return [
            candidate.name
            for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm"))
            if candidate.exists()
        ]

    def server_connections(self) -> int | None:
        return None if self.server is None else self.server.connection_count()

    def close(self) -> None:
        for name in list(self.created):
            if self.server is not None:
                self.server.drop_database(name)
        self.created.clear()


def record(
    report: Report,
    workload: str,
    count: int,
    seconds: float,
    kind: DatabaseKind,
    **extra: float | int | bool | str | None,
) -> None:
    report.samples.append(
        Sample(f"{kind.value}_{workload}", 0, count, seconds, {**extra})
    )


def gate(report: Report, name: str, kind: DatabaseKind, passed: bool) -> None:
    report.gates[f"{kind.value}_{name}"] = passed


class CheckoutBalance:
    def __init__(self, database: Database) -> None:
        self.outstanding = 0
        self.checkouts = 0
        self.invalidations = 0
        for engine in {database.writer_engine, database.reader_engine}:
            event.listen(engine.sync_engine, "checkout", self._checkout)
            event.listen(engine.sync_engine, "checkin", self._checkin)
            event.listen(engine.sync_engine, "invalidate", self._invalidate)

    def _checkout(self, *arguments: object) -> None:
        self.outstanding += 1
        self.checkouts += 1

    def _checkin(self, *arguments: object) -> None:
        self.outstanding -= 1

    def _invalidate(self, *arguments: object) -> None:
        self.invalidations += 1


class StatementWatch:
    def __init__(self, database: Database, fragment: str) -> None:
        self.fragment = fragment
        self.started = asyncio.Event()
        self.observed = 0
        event.listen(
            database.writer_engine.sync_engine, "before_cursor_execute", self._observe
        )

    def _observe(
        self, connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if self.fragment in statement:
            self.observed += 1
            self.started.set()


def slow_statement(kind: DatabaseKind) -> str:
    if kind is DatabaseKind.SQLITE:
        return SLOW_SQLITE_STATEMENT
    return SLOW_POSTGRESQL_STATEMENT
