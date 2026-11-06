import errno
import os
import sys
import threading
from queue import Queue
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
from kora.runner.registry import synchronize

JOURNAL_NAME: Final[str] = "journal.jsonl"
SYNC_DEADLINE_NS: Final[int] = 5_000_000_000
RECORD_LIMIT: Final[int] = 1_048_576
RECORD_DEPTH: Final[int] = 8


class _Write:
    def __init__(self, line: bytes) -> None:
        self.line = line
        self.done = threading.Event()
        self.synced = False
        self.written = False


class RunningJournal:
    def __init__(self, directory: DirectoryAnchor, clock: Clock) -> None:
        self._directory = directory
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: Queue[_Write | None] = Queue()
        self._fd: int | None = None
        self._next = 0
        self._closed = False
        self._not_durable = 0
        self._thread: threading.Thread | None = None

    @property
    def not_durable(self) -> int:
        with self._lock:
            return self._not_durable

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def append(self, kind: str, data: JsonObject) -> bool:
        with self._lock:
            if self._closed:
                return False
            starting = self._fd is None
            if starting:
                try:
                    self._fd = open_private_file(
                        self._directory,
                        JOURNAL_NAME,
                        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    )
                except OSError:
                    self._closed = True
                    return False
            index = self._next
            line = canonical_bytes(
                {
                    "i": index,
                    "at": instant_wire(self._clock.now()),
                    "kind": kind,
                    "data": data,
                }
            )
            job = _Write(line + b"\n")
            self._jobs.put(job)
        if starting:
            self._thread = threading.Thread(
                target=self._write, name="kora-journal", daemon=True
            )
            self._thread.start()
        return self._wait(job, index)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._jobs.put(None)

    def _wait(self, job: _Write, index: int) -> bool:
        deadline = self._clock.monotonic_ns() + SYNC_DEADLINE_NS
        while not job.done.is_set():
            remaining = deadline - self._clock.monotonic_ns()
            if remaining <= 0:
                break
            job.done.wait(remaining / 1_000_000_000)
        with self._lock:
            if job.synced:
                self._next = index + 1
                return True
            self._closed = True
            if job.written or not job.done.is_set():
                self._not_durable += 1
            return False

    def _write(self) -> None:
        try:
            synchronize(self._directory.descriptor)
        except OSError:
            with self._lock:
                self._closed = True
        while True:
            job = self._jobs.get()
            if job is None:
                return
            with self._lock:
                closed = self._closed
                descriptor = self._fd
            if closed or descriptor is None:
                job.done.set()
                continue
            try:
                self._store(descriptor, job)
                job.synced = True
            except OSError:
                with self._lock:
                    self._closed = True
            finally:
                job.done.set()

    def _store(self, descriptor: int, job: _Write) -> None:
        view = memoryview(job.line)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            view = view[written:]
        job.written = True
        if sys.platform == "darwin":
            synchronize(descriptor)
        else:
            os.fdatasync(descriptor)


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
