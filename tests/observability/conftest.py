import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    LoggingSettings,
    ProcessDiagnostics,
    configure_process,
    current_diagnostics,
)

TEST_COMPONENT: str = CATALOG.register_component("testing")
NOTE: EventSpec = CATALOG.register(
    EventSpec(
        name="testing.note",
        component=TEST_COMPONENT,
        level=Level.INFO,
        message="a test note",
        fields={
            "text": FieldKind.STRING,
            "items": FieldKind.STRINGS,
            "detail": FieldKind.MAPPING,
            "count": FieldKind.INTEGER,
            "ratio": FieldKind.FLOAT,
            "flag": FieldKind.BOOLEAN,
            "exception": FieldKind.EXCEPTION,
        },
    )
)
LEVEL_EVENTS: dict[Level, EventSpec] = {
    level: CATALOG.register(
        EventSpec(
            name=f"testing.{level.name.lower()}",
            component=TEST_COMPONENT,
            level=level,
            message=f"a {level.name.lower()} note",
            fields={"text": FieldKind.STRING},
        )
    )
    for level in Level
}
INSTANCE_ID: str = "test-instance"
WAIT_SECONDS: float = 10.0


@dataclass(frozen=True)
class ProcessResult:
    code: int
    stdout: bytes
    stderr: bytes

    def stderr_lines(self) -> list[str]:
        return self.stderr.decode("utf-8").splitlines()

    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.stderr_lines()]


def run_kora(arguments: list[str], environment: Mapping[str, str]) -> ProcessResult:
    return run_python(["-m", "kora", *arguments], environment)


def run_python(arguments: list[str], environment: Mapping[str, str]) -> ProcessResult:
    completed = subprocess.run(
        [sys.executable, *arguments],
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        timeout=60,
        check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


class PipeReader:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._buffer = bytearray()
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
                self._buffer.extend(data)
                self._condition.notify_all()

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


@dataclass
class Pipe:
    read_end: int
    write_end: int
    reader: PipeReader

    def close_write_end(self) -> None:
        if self.write_end >= 0:
            os.close(self.write_end)
            self.write_end = -1

    def close_read_end(self) -> None:
        if self.read_end >= 0:
            os.close(self.read_end)
            self.read_end = -1


@pytest.fixture
def pipe() -> Iterator[Pipe]:
    read_end, write_end = os.pipe()
    created = Pipe(read_end, write_end, PipeReader(read_end))
    yield created
    created.close_write_end()
    created.close_read_end()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


Configure = Callable[[LoggingSettings, int], ProcessDiagnostics]


@pytest.fixture
def configure(clock: FakeClock) -> Iterator[Configure]:
    created: list[ProcessDiagnostics] = []

    def configure_with(
        settings: LoggingSettings, descriptor: int
    ) -> ProcessDiagnostics:
        diagnostics = configure_process(
            TEST_COMPONENT, settings, INSTANCE_ID, clock.now, descriptor
        )
        created.append(diagnostics)
        return diagnostics

    yield configure_with
    for diagnostics in created:
        diagnostics.shutdown(0.5)
    assert current_diagnostics() is None


def wait_until(
    condition: Callable[[], bool], deadline_seconds: float = WAIT_SECONDS
) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("the condition was not met before the deadline")
        time.sleep(0.005)
