import asyncio
import hashlib
import os
import stat
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Final, Protocol

from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.observability.redaction import StreamMasker
from kora.plugins.runtime.local.channel import MAX_LOG_BYTES, LogName, delivery_error
from kora.protocols.time import Clock
from kora.runner.channels import MAX_FRAME_BYTES, ChannelError, wait_future
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.registry import synchronize

TAIL_BYTES: Final[int] = 4096
QUEUE_BYTES: Final[int] = 4_194_304
RESUME_BYTES: Final[int] = QUEUE_BYTES // 2
SEAL_NS: Final[int] = 5_000_000_000
LOG_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
COMPONENT: Final[str] = CATALOG.register_component("runtime.local.runner")
CAPTURE: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.capture",
        COMPONENT,
        Level.INFO,
        "the attempt runner settled a command output capture",
        {
            "outcome": FieldKind.STRING,
            "retained_bytes": FieldKind.INTEGER,
            "observed_bytes": FieldKind.INTEGER,
            "waited_ms": FieldKind.INTEGER,
        },
    )
)


class CaptureOutcome(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    WRITE_FAILED = "write_failed"
    UNSEALED = "unsealed"


class CaptureIO(Protocol):
    def write(self, descriptor: int, data: memoryview) -> int: ...

    def sync(self, descriptor: int) -> None: ...


class NativeCaptureIO:
    def write(self, descriptor: int, data: memoryview) -> int:
        return os.write(descriptor, data)

    def sync(self, descriptor: int) -> None:
        synchronize(descriptor)


@dataclass(frozen=True)
class CaptureFacts:
    payload_digest: str
    size: int
    observed_bytes: int
    eof: bool
    truncated: bool
    tail: bytes
    identity: tuple[int, int] | None


@dataclass(frozen=True)
class LogFile:
    descriptor: int
    identity: tuple[int, int]


Maskers = Callable[[Callable[[str], None]], StreamMasker]


def create_log(attempt: DirectoryAnchor, which: LogName) -> LogFile | None:
    try:
        descriptor = os.open(
            f"{which}.log", LOG_FLAGS, 0o600, dir_fd=attempt.descriptor
        )
    except OSError:
        return None
    try:
        information = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        return None
    return LogFile(descriptor, (information.st_dev, information.st_ino))


@dataclass(frozen=True)
class OpenedLog:
    descriptor: int
    size: int
    sha256: str
    offset: int


class _Stream:
    def __init__(
        self, which: LogName, descriptor: int, log: LogFile | None, maskers: Maskers
    ) -> None:
        self.which: LogName = which
        self.descriptor: int | None = descriptor
        self.pieces: list[str] = []
        self.masker = maskers(self.pieces.append)
        self.reading = False
        self.observed = 0
        self.accepted = 0
        self.retaining = True
        self.capped = False
        self.eof = False
        self.log: int | None = None if log is None else log.descriptor
        self.identity: tuple[int, int] | None = None if log is None else log.identity
        self.size = 0
        self.digest = hashlib.sha256()
        self.tail = bytearray()
        self.failed = log is None


class CommandCapture:
    def __init__(
        self,
        attempt: DirectoryAnchor,
        stdout: int,
        stderr: int,
        logs: tuple[LogFile | None, LogFile | None],
        maskers: Maskers,
        clock: Clock,
        log: ComponentLogger,
        io: CaptureIO,
    ) -> None:
        owned = [stdout, stderr, *(item.descriptor for item in logs if item)]
        try:
            self._loop = asyncio.get_running_loop()
            self._streams = (
                _Stream("stdout", stdout, logs[0], maskers),
                _Stream("stderr", stderr, logs[1], maskers),
            )
        except BaseException:
            close_all(owned)
            raise
        self._clock = clock
        self._log = log
        self._io = io
        self._state = threading.Condition()
        self._queue: deque[tuple[_Stream, bytes]] = deque()
        self._queued = 0
        self._high_water = 0
        self._paused = False
        self._draining = False
        self._sync_requested = False
        self._closed = False
        self._settled = False
        self._facts: dict[str, CaptureFacts] | None = None
        self._ended: asyncio.Future[None] = self._loop.create_future()
        self._synced: asyncio.Future[bool] = self._loop.create_future()
        directory: int | None = None
        try:
            directory = os.dup(attempt.descriptor)
            for descriptor in (stdout, stderr):
                os.set_blocking(descriptor, False)
            for stream in self._streams:
                self._start_reading(stream)
            self.writer = threading.Thread(
                target=self._write, args=(directory,), name="kora-capture", daemon=True
            )
        except BaseException:
            for stream in self._streams:
                self._stop_reading(stream)
            close_all([*owned, *([] if directory is None else [directory])])
            raise
        try:
            self.writer.start()
        except (RuntimeError, MemoryError):
            close_all([directory, *(item.descriptor for item in logs if item)])
            with self._state:
                for stream in self._streams:
                    stream.failed = True
                    stream.log = None
            self._deliver(False)

    @property
    def settled(self) -> bool:
        with self._state:
            return self._settled

    @property
    def high_water(self) -> int:
        with self._state:
            return self._high_water

    @property
    def queued(self) -> int:
        with self._state:
            return self._queued

    def observed(self, which: LogName) -> int:
        return self._stream(which).observed

    @property
    def ended(self) -> bool:
        return self._ended.done()

    def facts(self, which: LogName) -> CaptureFacts | None:
        with self._state:
            return None if self._facts is None else self._facts.get(which)

    def omitted(self) -> tuple[tuple[LogName, int], ...]:
        return tuple(
            (stream.which, stream.observed - stream.accepted)
            for stream in self._streams
            if stream.capped
        )

    def _stream(self, which: LogName) -> _Stream:
        return next(stream for stream in self._streams if stream.which == which)

    def _start_reading(self, stream: _Stream) -> None:
        if stream.descriptor is not None and not stream.reading:
            self._loop.add_reader(stream.descriptor, self._readable, stream)
            stream.reading = True

    def _stop_reading(self, stream: _Stream) -> None:
        if stream.descriptor is not None and stream.reading:
            self._loop.remove_reader(stream.descriptor)
            stream.reading = False

    def _readable(self, stream: _Stream) -> None:
        descriptor = stream.descriptor
        if descriptor is None:
            return
        try:
            chunk = os.read(descriptor, MAX_FRAME_BYTES)
        except BlockingIOError:
            return
        except OSError:
            self._end(stream, eof=False)
            return
        if chunk:
            stream.masker.feed(chunk)
            self._offer(stream)
        else:
            self._end(stream, eof=True)

    def _end(self, stream: _Stream, *, eof: bool) -> None:
        descriptor = stream.descriptor
        if descriptor is None:
            return
        self._stop_reading(stream)
        stream.descriptor = None
        stream.eof = eof
        os.close(descriptor)
        stream.masker.close()
        self._offer(stream)
        if all(item.descriptor is None for item in self._streams):
            if not self._ended.done():
                self._ended.set_result(None)

    def _offer(self, stream: _Stream) -> None:
        data = "".join(stream.pieces).encode("utf-8")
        stream.pieces.clear()
        stream.observed += len(data)
        if not data or not stream.retaining:
            return
        piece = data[: MAX_LOG_BYTES - stream.accepted]
        if len(piece) < len(data):
            stream.capped = True
            stream.retaining = False
        if not piece:
            return
        with self._state:
            if (
                stream.failed
                or self._closed
                or (self._draining and self._queued >= QUEUE_BYTES)
            ):
                stream.retaining = False
                return
            self._queue.append((stream, piece))
            self._queued += len(piece)
            self._high_water = max(self._high_water, self._queued)
            self._state.notify_all()
            pause = not self._draining and self._queued >= QUEUE_BYTES
            self._paused = self._paused or pause
        stream.accepted += len(piece)
        if pause:
            for item in self._streams:
                self._stop_reading(item)

    def _resume(self) -> None:
        with self._state:
            if not self._paused or (not self._draining and self._queued > RESUME_BYTES):
                return
            self._paused = False
        for stream in self._streams:
            self._start_reading(stream)

    def _call(self, callback: Callable[[], None]) -> None:
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError:
            if not self._loop.is_closed():
                raise

    def _write(self, directory: int) -> None:
        try:
            while True:
                with self._state:
                    self._state.wait_for(
                        lambda: (
                            self._closed or self._sync_requested or bool(self._queue)
                        )
                    )
                    if self._closed:
                        return
                    if not self._queue:
                        break
                    stream, data = self._queue[0]
                self._store(stream, data)
                with self._state:
                    if self._closed:
                        return
                    self._queue.popleft()
                    self._queued -= len(data)
                    wake = self._paused and self._queued <= RESUME_BYTES
                if wake:
                    self._call(self._resume)
            self._call(partial(self._deliver, self._sync(directory)))
        finally:
            for stream in self._streams:
                if stream.log is not None:
                    os.close(stream.log)
            os.close(directory)

    def _deliver(self, synced: bool) -> None:
        if not self._synced.done():
            self._synced.set_result(synced)

    def _store(self, stream: _Stream, data: bytes) -> None:
        view = memoryview(data)
        while view and stream.log is not None and not stream.failed:
            try:
                written = self._io.write(stream.log, view)
            except OSError:
                written = 0
            if written <= 0:
                with self._state:
                    stream.failed = True
                return
            stream.digest.update(view[:written])
            with self._state:
                stream.size += written
                stream.tail += view[max(0, written - TAIL_BYTES) : written]
                del stream.tail[:-TAIL_BYTES]
            view = view[written:]

    def _sync(self, directory: int) -> bool:
        try:
            for stream in self._streams:
                if stream.log is not None:
                    self._io.sync(stream.log)
            self._io.sync(directory)
        except OSError:
            return False
        return True

    async def seal(self, started: int) -> None:
        deadline = started + SEAL_NS
        sealed = False
        try:
            with self._state:
                self._draining = True
            self._resume()
            try:
                await wait_future(self._ended, deadline, self._clock)
            except ChannelError:
                pass
            self._cut()
            with self._state:
                self._sync_requested = True
                self._state.notify_all()
            try:
                await wait_future(
                    self._synced, self._clock.monotonic_ns() + SEAL_NS, self._clock
                )
            except ChannelError:
                pass
            else:
                sealed = self._synced.result()
        finally:
            self._settle(sealed, started)

    def close(self) -> None:
        self._cut()
        self._settle(False, self._clock.monotonic_ns())

    def _cut(self) -> None:
        for stream in self._streams:
            self._end(stream, eof=False)

    def _settle(self, sealed: bool, started: int) -> None:
        with self._state:
            if self._settled:
                return
            self._settled = True
            self._closed = True
            self._queue.clear()
            self._queued = 0
            self._state.notify_all()
            outcomes = {
                stream.which: self._outcome(stream, sealed) for stream in self._streams
            }
            if sealed:
                self._facts = {
                    stream.which: self._fact(stream) for stream in self._streams
                }
        waited = max(0, (self._clock.monotonic_ns() - started) // 1_000_000)
        for stream in self._streams:
            outcome = outcomes[stream.which]
            level = Level.INFO if outcome is CaptureOutcome.COMPLETE else Level.WARNING
            self._log.emit_at(
                level,
                CAPTURE,
                outcome=outcome.value,
                retained_bytes=stream.size if sealed else 0,
                observed_bytes=stream.observed,
                waited_ms=waited,
            )

    def _outcome(self, stream: _Stream, sealed: bool) -> CaptureOutcome:
        if not sealed:
            return CaptureOutcome.UNSEALED
        if stream.failed:
            return CaptureOutcome.WRITE_FAILED
        if truncated(stream):
            return CaptureOutcome.TRUNCATED
        return CaptureOutcome.COMPLETE

    def _fact(self, stream: _Stream) -> CaptureFacts:
        return CaptureFacts(
            payload_digest="sha256:" + stream.digest.hexdigest(),
            size=stream.size,
            observed_bytes=stream.observed,
            eof=stream.eof,
            truncated=truncated(stream),
            tail=bytes(stream.tail),
            identity=stream.identity,
        )


def truncated(stream: _Stream) -> bool:
    return stream.observed > stream.size or not stream.eof


def close_all(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        os.close(descriptor)


def open_log(
    attempt: DirectoryAnchor, which: LogName, facts: CaptureFacts, offset: int
) -> OpenedLog:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(f"{which}.log", flags, dir_fd=attempt.descriptor)
    except OSError:
        raise delivery_error("transfer_refused") from None
    information = os.fstat(descriptor)
    if (
        not stat.S_ISREG(information.st_mode)
        or (information.st_dev, information.st_ino) != facts.identity
    ):
        os.close(descriptor)
        raise delivery_error("transfer_refused")
    return OpenedLog(descriptor, facts.size, facts.payload_digest, offset)


def read_log_frame(opened: OpenedLog, position: int, length: int) -> bytes:
    try:
        piece = os.pread(opened.descriptor, length, position)
    except OSError:
        raise delivery_error("integrity_error") from None
    if len(piece) != length:
        raise delivery_error("integrity_error")
    return piece
