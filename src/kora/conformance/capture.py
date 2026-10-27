import json
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

from kora.observability import (
    LoggingSettings,
    ProcessDiagnostics,
    configure_process,
    current_diagnostics,
)
from kora.observability.bootstrap import Clock
from kora.protocols.secret_value import SecretScope

WAIT_SECONDS: Final[float] = 10.0
MAX_CAPTURE_BYTES: Final[int] = 1_048_576


class CaptureTimeout(TimeoutError):
    def __init__(self, stage: str) -> None:
        super().__init__(f"the diagnostics capture did not finish: {stage}")


class CaptureLeftover(RuntimeError):
    def __init__(self) -> None:
        super().__init__("an earlier capture timed out and still holds diagnostics")


class CaptureOverflow(AssertionError):
    def __init__(self, discarded: int) -> None:
        super().__init__(f"the capture discarded {discarded} bytes past its cap")


class PipeReader:
    def __init__(self, descriptor: int, max_bytes: int = MAX_CAPTURE_BYTES) -> None:
        self._descriptor = descriptor
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._discarded = 0
        self._condition = threading.Condition()
        self._closed = False
        self._chunk = 65536
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, chunk: int = 65536) -> None:
        self._chunk = chunk
        self._thread.start()

    def _run(self) -> None:
        while True:
            data = os.read(self._descriptor, self._chunk)
            with self._condition:
                if not data:
                    self._closed = True
                    self._condition.notify_all()
                    return
                room = self._max_bytes - len(self._buffer)
                self._buffer.extend(data[: max(room, 0)])
                self._discarded += max(len(data) - max(room, 0), 0)
                self._condition.notify_all()

    @property
    def discarded(self) -> int:
        with self._condition:
            return self._discarded

    @property
    def data(self) -> bytes:
        with self._condition:
            return bytes(self._buffer)

    def lines(self) -> list[str]:
        with self._condition:
            return self._buffer.decode("utf-8").splitlines()

    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.lines() if line.startswith("{")]

    def wait_for_lines(
        self, count: int, deadline_seconds: float = WAIT_SECONDS
    ) -> None:
        deadline = time.monotonic() + deadline_seconds
        with self._condition:
            while self._buffer.count(b"\n") < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"fewer than {count} lines were written")
                self._condition.wait(remaining)

    def wait_closed(self, deadline_seconds: float = WAIT_SECONDS) -> None:
        deadline = time.monotonic() + deadline_seconds
        with self._condition:
            while not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("the pipe was not closed")
                self._condition.wait(remaining)

    def join(self, deadline_seconds: float = WAIT_SECONDS) -> None:
        if self._thread.ident is None:
            return
        self._thread.join(deadline_seconds)
        if self._thread.is_alive():
            raise TimeoutError("the pipe reader did not stop")


@dataclass
class Pipe:
    read_end: int
    write_end: int
    reader: PipeReader

    @classmethod
    def open(cls, max_bytes: int = MAX_CAPTURE_BYTES) -> "Pipe":
        read_end, write_end = os.pipe()
        return cls(read_end, write_end, PipeReader(read_end, max_bytes))

    def close_write_end(self) -> None:
        if self.write_end >= 0:
            os.close(self.write_end)
            self.write_end = -1

    def close_read_end(self) -> None:
        if self.read_end >= 0:
            os.close(self.read_end)
            self.read_end = -1

    def close(self) -> None:
        self.close_write_end()
        self.close_read_end()

    def release(self) -> None:
        self.close_write_end()
        self.reader.join()
        self.close_read_end()


@dataclass
class Leftover:
    diagnostics: ProcessDiagnostics
    pipe: Pipe


@dataclass
class LeftoverSlot:
    held: list[Leftover] = field(default_factory=list[Leftover])

    def clear_finished(self) -> None:
        for leftover in list(self.held):
            if not leftover.diagnostics.writer_is_alive():
                release_leftover(leftover)
                self.held.remove(leftover)
        if self.held:
            raise CaptureLeftover


def release_leftover(leftover: Leftover) -> None:
    try:
        leftover.pipe.release()
    except TimeoutError as error:
        raise CaptureTimeout("a leftover pipe reader did not stop") from error


LEFTOVERS: Final[LeftoverSlot] = LeftoverSlot()


@dataclass
class Capture:
    diagnostics: ProcessDiagnostics
    scope: SecretScope
    pipe: Pipe
    deadline_seconds: float
    finished: bool = False

    @property
    def reader(self) -> PipeReader:
        return self.pipe.reader

    def finish(self) -> list[dict[str, object]]:
        if not self.finished:
            self.finished = True
            self.release()
        return self.reader.records()

    def release(self) -> None:
        closed = False
        try:
            self.scope.release()
            drained = self.diagnostics.shutdown(self.deadline_seconds)
            if self.diagnostics.writer_is_alive():
                raise CaptureTimeout("the diagnostics writer is still running")
            self.pipe.close_write_end()
            self.stop_reader()
            self.pipe.close_read_end()
            closed = True
        finally:
            if not closed:
                LEFTOVERS.held.append(Leftover(self.diagnostics, self.pipe))
        if current_diagnostics() is not None:
            raise CaptureTimeout("diagnostics are still configured")
        if not drained:
            raise CaptureTimeout("the diagnostics queue did not drain")
        if self.reader.discarded:
            raise CaptureOverflow(self.reader.discarded)

    def stop_reader(self) -> None:
        try:
            self.reader.wait_closed(self.deadline_seconds)
            self.reader.join(self.deadline_seconds)
        except TimeoutError as error:
            raise CaptureTimeout("the pipe reader did not stop") from error


@contextmanager
def captured(
    component: str,
    settings: LoggingSettings,
    instance_id: str,
    clock: Clock,
    deadline_seconds: float = 5.0,
    max_bytes: int = MAX_CAPTURE_BYTES,
) -> Generator[Capture]:
    LEFTOVERS.clear_finished()
    pipe = Pipe.open(max_bytes)
    configured = False
    try:
        diagnostics = configure_process(
            component, settings, instance_id, clock, pipe.write_end
        )
        configured = True
    finally:
        if not configured:
            pipe.close()
    capture = Capture(diagnostics, SecretScope(diagnostics), pipe, deadline_seconds)
    try:
        pipe.reader.start()
        yield capture
    finally:
        if not capture.finished:
            capture.finished = True
            capture.release()
