from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from threading import BoundedSemaphore
from typing import ParamSpec, TypeVar

from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool.base import ConnectionPoolEntry

from kora.db.completion import complete, finish_releasing
from kora.db.connection import (
    LocalSQLiteWriterEntry,
    SqliteDriverConnection,
    open_sqlite_file,
)
from kora.db.custody_journal import CustodySQLiteInstallation, native_statement
from kora.db.custody_models import CustodyParticipantRegistration, CustodyUnavailable
from kora.db.custody_native import (
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    custody_native_job,
)
from kora.db.custody_sqlite import (
    CustodySQLiteConnection,
    CustodySQLiteRoot,
    SQLiteWorkerOriginal,
)
from kora.db.identifiers import is_identifier
from kora.db.maintenance import sqlite_record_worker
from kora.db.token_statements import REVOKED, revoke_token_statement
from kora.db.transactions import WriterTransaction, WriterTransactionError
from kora.sqlite.driver import Connection

DriverArgs = ParamSpec("DriverArgs")
Result = TypeVar("Result")
LOCAL_SQLITE_NATIVE_SEQUENCE = count(1)


class CustodyDriverConnection(SqliteDriverConnection):
    native_candidate: CustodySQLiteConnection | None = None
    _joined: bool = False
    _on_retired: Callable[[], None] | None = None

    def __init__(self, connector: Callable[[], Connection]) -> None:
        super().__init__(connector)
        self._loop = asyncio.get_running_loop()

    @property
    def retired(self) -> bool:
        native = self.native_candidate
        return self._joined and (native is None or native.settled())

    def observe_retirement(self, connection: Connection) -> None:
        if connection is self.native_candidate:
            try:
                self._loop.call_soon_threadsafe(self._resume_if_retired)
            except RuntimeError:
                if not self._loop.is_closed():
                    raise

    def after_retirement(self, resume: Callable[[], None]) -> None:
        if self._joined and self.native_candidate is not None:
            self._on_retired = resume
            self._resume_if_retired()

    def _resume_if_retired(self) -> None:
        resume = self._on_retired
        if resume is not None and self.retired:
            self._on_retired = None
            resume()

    async def bind_custody_root(
        self, installation: CustodySQLiteInstallation, journal_id: bytes
    ) -> CustodySQLiteRoot:
        native = self.native_candidate
        if native is None:
            raise CustodyUnavailable
        return await self.custody_worker(
            lambda: native.bind_root(
                installation.anchor,
                installation.metadata_id,
                journal_id,
                installation.frontier,
            )
        )

    async def custody_worker(self, work: Callable[[], Result]) -> Result:
        return await self._execute(work)

    async def _execute(
        self,
        work: Callable[DriverArgs, Result],
        *args: DriverArgs.args,
        **kwargs: DriverArgs.kwargs,
    ) -> Result:
        def guarded() -> Result:
            with custody_native_job():
                return work(*args, **kwargs)

        return await super()._execute(guarded)

    async def close(self) -> None:
        await complete(self._close_and_join())

    async def _close_and_join(self) -> None:
        try:
            await super().close()
        finally:
            if self._thread.ident is not None:
                await asyncio.to_thread(self._thread.join)
            self._joined = True


class SQLitePhysicalWriter:
    def __init__(self, root: CustodyNativeDirectory) -> None:
        self.root = root
        self.installation: CustodySQLiteInstallation | None = None
        self.driver: CustodyDriverConnection | None = None
        self.returned: CustodyDriverConnection | None = None

    def require_retirement(self) -> None:
        if self.driver is not None and not self.driver.retired:
            raise CustodyUnavailable

    def allocate(self, retire: Callable[[Connection], None]) -> CustodySQLiteConnection:
        if self.driver is None:
            raise CustodyUnavailable
        candidate = CustodySQLiteConnection(retire)
        self.driver.native_candidate = candidate
        return candidate

    def admit(self, descriptor: int, empty: int) -> bool:
        driver, installation = self.driver, self.installation
        if driver is None or installation is None or driver.native_candidate is None:
            raise CustodyUnavailable
        return driver.native_candidate.admit_main(
            installation.anchor, self.root.require_private_acl, descriptor, empty
        )

    def connector(
        self, path: Path, slots: BoundedSemaphore
    ) -> Callable[[], Awaitable[CustodyDriverConnection]]:
        def open_original() -> Connection:
            with custody_native_job():
                return open_sqlite_file(path, slots, custody=self)

        async def connect() -> CustodyDriverConnection:
            self.require_retirement()
            driver = CustodyDriverConnection(open_original)
            self.driver = driver

            async def start() -> CustodyDriverConnection:
                try:
                    await driver
                except BaseException:
                    await driver.close()
                    raise
                return driver

            return await finish_releasing(start(), CustodyDriverConnection.close)

        return connect

    def checkin(
        self, connection: DBAPIConnection | None, record: ConnectionPoolEntry
    ) -> None:
        if connection is not None and sqlite_record_worker(record) is self.driver:
            self.returned = self.driver

    def observe_retirement(self, connection: Connection) -> None:
        if self.driver is not None:
            self.driver.observe_retirement(connection)

    @property
    def released(self) -> bool:
        return (
            self.driver is self.returned or self.driver is None or self.driver.retired
        )


@dataclass
class LocalSQLiteWriterScope:
    root: CustodyNativeDirectory
    lifetime: CustodyLifetimeLease
    participant: CustodyParticipantRegistration
    entry: LocalSQLiteWriterEntry = field(default_factory=LocalSQLiteWriterEntry)
    physical: SQLitePhysicalWriter = field(init=False)

    def __post_init__(self) -> None:
        self.physical = SQLitePhysicalWriter(self.root)


class LocalSQLiteWriterLoan:
    def __init__(
        self, logical: WriterTransaction, worker: SQLiteWorkerOriginal
    ) -> None:
        self._logical = logical
        self._worker = worker
        self._open = True
        self._busy = False

    @classmethod
    @contextmanager
    def lend(
        cls, logical: WriterTransaction, worker: SQLiteWorkerOriginal
    ) -> Generator[LocalSQLiteWriterLoan]:
        loan = cls(logical, worker)
        try:
            yield loan
            loan._require_open()
        finally:
            loan._open = False

    def _require_open(self) -> None:
        if not self._open or self._busy:
            raise CustodyUnavailable
        try:
            self._logical.require_active()
        except WriterTransactionError:
            raise CustodyUnavailable from None

    def claim_lock_context(self) -> None:
        self._require_open()
        self._logical.claim_lock_context()

    async def revoke_token(
        self, token_id: str, next_token_id: str | None = None
    ) -> int:
        self._require_open()
        if not is_identifier(token_id) or (
            next_token_id is not None and not is_identifier(next_token_id)
        ):
            raise CustodyUnavailable
        self._busy = True
        try:
            return await complete(
                self._worker.driver.custody_worker(
                    lambda: self._revoke_native(token_id, next_token_id)
                )
            )
        except (sqlite3.Error, CustodyUnavailable, asyncio.CancelledError):
            self._open = False
            raise
        finally:
            self._busy = False

    def _revoke_native(self, token_id: str, next_token_id: str | None) -> int:
        statement, _ = native_statement(revoke_token_statement())
        revoked = REVOKED.isoformat(timespec="microseconds")
        rows = self._worker.root.mutate(
            statement, {"token_id": token_id, "revoked": revoked}
        )
        if next_token_id is not None:
            rows += self._worker.root.mutate(
                statement, {"token_id": next_token_id, "revoked": revoked}
            )
        return rows
