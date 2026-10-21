import os
import time
from collections.abc import Callable, Iterator, Mapping

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
from tests.support.observability import (
    WAIT_SECONDS,
    FakeClock,
    Pipe,
    PipeReader,
    ProcessResult,
    run_python,
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


def run_kora(arguments: list[str], environment: Mapping[str, str]) -> ProcessResult:
    return run_python(["-m", "kora", *arguments], environment)


@pytest.fixture
def pipe() -> Iterator[Pipe]:
    read_end, write_end = os.pipe()
    created = Pipe(read_end, write_end, PipeReader(read_end))
    yield created
    created.close_write_end()
    created.reader.join()
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
