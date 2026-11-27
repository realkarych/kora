from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from enum import Enum, auto
from hashlib import sha256
from pathlib import Path
from threading import Thread, current_thread
from typing import Protocol, TypeVar
from uuid import UUID, uuid4

from kora.db.custody_models import (
    CustodyCommitPhase,
    CustodyFileAnchor,
    CustodyFrontier,
    CustodyIssuerKind,
    CustodyNativeOutcome,
    CustodyPendingCommit,
    CustodyUnavailable,
)
from kora.db.custody_native import file_anchor, require_native_file
from kora.db.types import SQLITE_SYNCHRONOUS_FULL
from kora.sqlite.driver import Connection, Cursor

_READ_FRONTIER = (
    "SELECT singleton FROM main.custody_frontier "
    "WHERE singleton = 1 AND format_version = 1 AND metadata_id = ? AND journal_id = ? "
    "AND generation = ? AND commit_sequence = ? AND token = ?"
)
_ADVANCE_FRONTIER = (
    "UPDATE main.custody_frontier SET generation = ?, commit_sequence = ?, token = ?, "
    "previous_generation = ?, previous_commit = ?, previous_token = ? "
    "WHERE singleton = 1 AND format_version = 1 AND metadata_id = ? AND journal_id = ? "
    "AND generation = ? AND commit_sequence = ? AND token = ?"
)


class _RootStage(Enum):
    NEW = auto()
    ARMED = auto()
    ADVANCED = auto()
    REVOKED = auto()


class CustodySQLiteConnection(Connection):
    root: CustodySQLiteRoot | None = None
    _anchor: CustodyFileAnchor | None = None

    def admit_main(
        self,
        anchor: CustodyFileAnchor,
        require_acl: Callable[[int], None],
        descriptor: int,
        empty: int,
    ) -> bool:
        path = Path(os.fsdecode(anchor.canonical_path))
        require_sqlite_anchor(path, anchor)
        native = require_native_file(descriptor, directory=False)
        if (
            empty
            or file_anchor(native, path) != anchor
            or fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR
        ):
            raise CustodyUnavailable
        require_acl(descriptor)
        self._anchor = anchor
        return True

    def require_anchor(self, anchor: CustodyFileAnchor) -> Path:
        if self._anchor != anchor:
            raise CustodyUnavailable
        path = Path(os.fsdecode(anchor.canonical_path))
        require_sqlite_anchor(path, anchor)
        return path

    def bind_root(
        self,
        anchor: CustodyFileAnchor,
        metadata_id: bytes,
        journal_id: bytes,
        frontier: CustodyFrontier,
    ) -> CustodySQLiteRoot:
        if self.root is not None or frontier.is_initial or not self.in_transaction:
            raise CustodyUnavailable
        root = CustodySQLiteRoot(self, anchor, metadata_id, journal_id, frontier)
        self.root = root
        with ExitStack() as cleanup:
            cleanup.callback(root.require_native_closed)
            cleanup.callback(root.rollback)
            root.arm()
            cleanup.pop_all()
        return root


def require_sqlite_anchor(path: Path, expected: CustodyFileAnchor) -> None:
    try:
        native = path.stat(follow_symlinks=False)
    except OSError:
        raise CustodyUnavailable from None
    if (
        not stat.S_ISREG(native.st_mode)
        or native.st_uid != os.geteuid()
        or stat.S_IMODE(native.st_mode) != 0o600
        or native.st_nlink != 1
        or file_anchor(native, path) != expected
    ):
        raise CustodyUnavailable


def _frontier_values(frontier: CustodyFrontier) -> tuple[int, int, bytes]:
    return frontier.generation, frontier.sequence, frontier.token


@dataclass(eq=False, repr=False)
class CustodySQLiteRoot:
    _connection: CustodySQLiteConnection
    _anchor: CustodyFileAnchor
    _metadata_id: bytes
    _journal_id: bytes
    _frontier: CustodyFrontier
    _worker: Thread = field(default_factory=current_thread, init=False)
    _nonce: UUID = field(default_factory=uuid4, init=False)
    _stage: _RootStage = field(default=_RootStage.NEW, init=False)
    _outcome: CustodyNativeOutcome | None = field(default=None, init=False)
    _rollback_issued: bool = field(default=False, init=False)
    settings_digest: bytes = field(init=False)

    @property
    def outcome(self) -> CustodyNativeOutcome | None:
        return self._outcome

    def arm(self) -> None:
        self._take_stage(_RootStage.NEW)
        path = self._require_active()
        with closing(self._execute("PRAGMA database_list")) as cursor:
            row: object = cursor.fetchone()
            if row != (0, "main", str(path)) or cursor.fetchone() is not None:
                raise CustodyUnavailable
        self.settings_digest = self._settings()
        with closing(self._execute(_READ_FRONTIER, self._frontier_key())) as cursor:
            if cursor.fetchone() != (1,):
                raise CustodyUnavailable
        self._control(f"SAVEPOINT custody_outer_{self._nonce.hex}")
        self._control(f"SAVEPOINT custody_inner_{self._nonce.hex}")
        self._stage = _RootStage.ARMED

    def _require_owner(self) -> None:
        if self._connection.root is not self or current_thread() is not self._worker:
            raise CustodyUnavailable

    def _require_active(self) -> Path:
        if not self._connection.in_transaction:
            raise CustodyUnavailable
        return self._connection.require_anchor(self._anchor)

    def _execute(
        self, statement: str, parameters: tuple[int | bytes, ...] | dict[str, str] = ()
    ) -> Cursor:
        try:
            return self._connection.execute(statement, parameters)
        except sqlite3.Error:
            raise CustodyUnavailable from None

    def _read_setting(self, statement: str) -> object:
        with closing(self._execute(statement)) as cursor:
            return cursor.fetchone()

    def _settings(self) -> bytes:
        mode = self._read_setting("PRAGMA main.journal_mode")
        synchronization = self._read_setting("PRAGMA main.synchronous")
        fullfsync = self._read_setting("PRAGMA fullfsync")
        if (
            mode != ("wal",)
            or synchronization != (SQLITE_SYNCHRONOUS_FULL,)
            or fullfsync not in ((0,), (1,))
            or (sys.platform == "darwin" and fullfsync != (1,))
        ):
            raise CustodyUnavailable
        sync = int(fullfsync == (1,))
        return sha256(f"sqlite:wal:{SQLITE_SYNCHRONOUS_FULL}:{sync}".encode()).digest()

    def _control(self, statement: str) -> None:
        self._execute(statement).close()

    def _take_stage(self, *expected: _RootStage) -> _RootStage:
        self._require_owner()
        stage = self._stage
        self._stage = _RootStage.REVOKED
        if stage not in expected:
            raise CustodyUnavailable
        return stage

    def require_original(self) -> None:
        stage = self._take_stage(_RootStage.ARMED, _RootStage.ADVANCED)
        self._require_active()
        if self._settings() != self.settings_digest:
            raise CustodyUnavailable
        self._stage = stage

    def mutate(self, sql: str, parameters: Mapping[str, str]) -> int:
        self._take_stage(_RootStage.ARMED)
        self._require_active()
        with closing(self._execute(sql, dict(parameters))) as cursor:
            count = cursor.rowcount
        self._require_active()
        self._stage = _RootStage.ARMED
        return count

    def _frontier_key(self) -> tuple[bytes, bytes, int, int, bytes]:
        return self._metadata_id, self._journal_id, *_frontier_values(self._frontier)

    def advance_frontier(self, pending: CustodyPendingCommit) -> None:
        self.require_original()
        self._take_stage(_RootStage.ARMED)
        previous = pending.previous
        binding = pending.transaction
        if (
            pending.issuer_kind is not CustodyIssuerKind.ORDINARY
            or pending.phase is not CustodyCommitPhase.PREPARED
            or pending.journal_id != self._journal_id
            or previous != self._frontier
            or binding.metadata_id != self._metadata_id
            or binding.postgres is not None
            or binding.settings_digest != self.settings_digest
        ):
            raise CustodyUnavailable
        self._control(f"RELEASE custody_inner_{self._nonce.hex}")
        self._require_active()
        values = (
            *_frontier_values(pending.frontier),
            *_frontier_values(previous),
            *self._frontier_key(),
        )
        with closing(self._execute(_ADVANCE_FRONTIER, values)) as cursor:
            if cursor.rowcount != 1:
                raise CustodyUnavailable
        self._require_active()
        self._stage = _RootStage.ADVANCED

    def commit(self) -> None:
        self.require_original()
        self._take_stage(_RootStage.ADVANCED)
        self._control(f"RELEASE custody_outer_{self._nonce.hex}")
        self._require_active()
        self._terminal("COMMIT", CustodyNativeOutcome.COMMITTED)

    def rollback(self) -> None:
        self._require_owner()
        self._stage = _RootStage.REVOKED
        if self._rollback_issued or self._outcome is CustodyNativeOutcome.COMMITTED:
            return
        self._rollback_issued = True
        if self._connection.in_transaction:
            self._terminal("ROLLBACK", CustodyNativeOutcome.ABORTED)
        elif self._outcome is None:
            self._outcome = CustodyNativeOutcome.ABORTED

    def _terminal(self, statement: str, observed: CustodyNativeOutcome) -> None:
        try:
            with closing(self._execute(statement)):
                if self._connection.in_transaction:
                    raise CustodyUnavailable
                if self._outcome is None:
                    self._outcome = observed
        finally:
            if self._outcome is None:
                self._outcome = CustodyNativeOutcome.UNKNOWN

    def require_native_closed(self) -> None:
        self._require_owner()
        if self._outcome is None or self._connection.in_transaction:
            raise CustodyUnavailable
        self._connection.root = None


_Result = TypeVar("_Result")


class SQLiteSerialWorker(Protocol):
    async def custody_worker(self, work: Callable[[], _Result]) -> _Result: ...


@dataclass(frozen=True)
class SQLiteWorkerOriginal:
    driver: SQLiteSerialWorker
    root: CustodySQLiteRoot

    @property
    def settings_digest(self) -> bytes:
        return self.root.settings_digest

    @property
    def outcome(self) -> CustodyNativeOutcome | None:
        return self.root.outcome

    async def require_original(self) -> None:
        await self.driver.custody_worker(self.root.require_original)

    async def advance_frontier(self, pending: CustodyPendingCommit) -> None:
        await self.driver.custody_worker(lambda: self.root.advance_frontier(pending))

    async def commit(self) -> None:
        await self.driver.custody_worker(self.root.commit)

    async def rollback(self) -> None:
        await self.driver.custody_worker(self.root.rollback)

    async def require_native_closed(self) -> None:
        await self.driver.custody_worker(self.root.require_native_closed)
