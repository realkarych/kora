import errno
import os
import sys
import threading
from collections import deque
from typing import Final

from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import JsonRefusal, canonical_bytes, decode_strict_json
from kora.protocols.identity import instant_wire
from kora.protocols.time import Clock
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_private_file,
    read_private_file,
)
from kora.runner.native_io import BorrowedInvocation, NativeIONotInvoked
from kora.runner.registry import synchronize

JOURNAL_NAME: Final[str] = "journal.jsonl"
SYNC_DEADLINE_NS: Final[int] = 5_000_000_000
RECORD_LIMIT: Final[int] = 1_048_576
RECORD_DEPTH: Final[int] = 8


class JournalCapacityError(Exception):
    pass


class _Write:
    def __init__(self, line: bytes, deadline: int) -> None:
        self.line = line
        self.deadline = deadline
        self.started = False
        self.done = False
        self.synced = False
        self.written = False


class RunningJournal:
    def __init__(self, directory: DirectoryAnchor, clock: Clock) -> None:
        self._directory = directory
        self._clock = clock
        self._state = threading.Condition()
        self._jobs: deque[_Write] = deque()
        self._next = 0
        self._prefix_bytes: int | None = None
        self._admitted_bytes = 0
        self._closed = False
        self._not_durable = 0
        self._thread: threading.Thread | None = None
        self._writer_ready = False

    @property
    def not_durable(self) -> int:
        with self._state:
            return self._not_durable

    @property
    def closed(self) -> bool:
        with self._state:
            return self._closed

    def append(
        self, kind: str, data: JsonObject, *, reader_bounded: bool = False
    ) -> bool:
        deadline = self._clock.monotonic_ns() + SYNC_DEADLINE_NS
        launch: tuple[threading.Thread, DirectoryAnchor, BorrowedInvocation] | None = (
            None
        )
        with self._state:
            if self._closed:
                return False
            line = canonical_bytes(
                {
                    "i": self._next,
                    "at": instant_wire(self._clock.now()),
                    "kind": kind,
                    "data": data,
                }
            )
            line += b"\n"
            if reader_bounded and (
                self._prefix_bytes is None
                or self._prefix_bytes + self._admitted_bytes + len(line) > RECORD_LIMIT
            ):
                raise JournalCapacityError
            job = _Write(line, deadline)
            self._jobs.append(job)
            self._next += 1
            self._admitted_bytes += len(line)
            if self._thread is None:
                try:
                    launch = self._prepare_writer()
                except (OSError, RuntimeError, MemoryError):
                    self._close_locked()
                    return False
                self._thread = launch[0]
            self._state.notify_all()
        if launch is not None:
            thread, directory, ownership = launch
            try:
                thread.start()
            except (RuntimeError, MemoryError):
                self.close()
                if ownership.seal() is None:
                    directory.close()
                return False
            with self._state:
                self._writer_ready = True
                self._state.notify_all()
        return self._wait(job)

    def _prepare_writer(
        self,
    ) -> tuple[threading.Thread, DirectoryAnchor, BorrowedInvocation]:
        ownership = BorrowedInvocation()
        descriptor = os.dup(self._directory.descriptor)
        try:
            directory = DirectoryAnchor(descriptor, self._directory.identity)
            thread = threading.Thread(
                target=self._write,
                args=(directory, ownership),
                name="kora-journal",
                daemon=True,
            )
            return thread, directory, ownership
        except (RuntimeError, MemoryError):
            os.close(descriptor)
            raise

    def close(self) -> None:
        with self._state:
            self._close_locked()

    def _close_locked(self) -> None:
        if not self._closed:
            self._closed = True
            self._jobs.clear()
            self._state.notify_all()

    def _wait(self, job: _Write) -> bool:
        with self._state:
            while not job.done and not self._closed:
                remaining = job.deadline - self._clock.monotonic_ns()
                if remaining <= 0:
                    break
                self._state.wait(remaining / 1_000_000_000)
            if job.synced:
                return True
            self._close_locked()
            if job.written or (job.started and not job.done):
                self._not_durable += 1
            return False

    def _write(self, directory: DirectoryAnchor, ownership: BorrowedInvocation) -> None:
        try:
            ownership.enter()
        except NativeIONotInvoked:
            return
        descriptor: int | None = None
        try:
            try:
                with self._state:
                    self._state.wait_for(lambda: self._closed or self._writer_ready)
                    if self._closed:
                        return
                    deadline = self._jobs[0].deadline
                if not self._available(deadline):
                    return
                directory.verify()
                if not self._available(deadline):
                    return
                descriptor = open_private_file(
                    directory, JOURNAL_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                )
                prefix_bytes = os.fstat(descriptor).st_size
                with self._state:
                    self._prefix_bytes = prefix_bytes
                if not self._available(deadline):
                    return
                synchronize(directory.descriptor)
            except (OSError, FilesystemError):
                self.close()
                return
            finally:
                directory.close()
            self._write_pending(descriptor)
        except OSError:
            self.close()
        finally:
            self.close()
            if descriptor is not None:
                os.close(descriptor)

    def _write_pending(self, descriptor: int) -> None:
        while True:
            with self._state:
                self._state.wait_for(lambda: self._closed or bool(self._jobs))
                if self._closed:
                    return
                job = self._jobs.popleft()
                if self._clock.monotonic_ns() >= job.deadline:
                    self._close_locked()
                    return
                job.started = True
            try:
                stored = self._store(descriptor, job)
            except OSError:
                stored = False
            with self._state:
                job.done = True
                job.synced = (
                    stored
                    and not self._closed
                    and self._clock.monotonic_ns() < job.deadline
                )
                if not job.synced:
                    self._close_locked()
                    return
                self._state.notify_all()

    def _available(self, deadline: int) -> bool:
        with self._state:
            return not self._closed and self._clock.monotonic_ns() < deadline

    def _store(self, descriptor: int, job: _Write) -> bool:
        view = memoryview(job.line)
        while view:
            if not self._available(job.deadline):
                return False
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            view = view[written:]
        with self._state:
            job.written = True
        if not self._available(job.deadline):
            return False
        if sys.platform == "darwin":
            synchronize(descriptor)
        else:
            os.fdatasync(descriptor)
        return True


def read_records(directory: DirectoryAnchor) -> tuple[tuple[int, str], ...]:
    try:
        raw = read_private_file(directory, JOURNAL_NAME, RECORD_LIMIT)
    except FileNotFoundError:
        return ()
    if raw and not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n")
        raw = b"" if cut < 0 else raw[: cut + 1]
    found: list[tuple[int, str]] = []
    for line in raw.splitlines():
        decoded = decode_strict_json(line, RECORD_LIMIT, RECORD_DEPTH)
        if isinstance(decoded, JsonRefusal) or not isinstance(decoded, dict):
            raise FilesystemError("journal_record")
        index = decoded.get("i")
        kind = decoded.get("kind")
        if type(index) is not int or type(kind) is not str:
            raise FilesystemError("journal_record")
        found.append((index, kind))
    return tuple(found)


def space_error(error: OSError) -> bool:
    return error.errno in (errno.ENOSPC, errno.EDQUOT)
