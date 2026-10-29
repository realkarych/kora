import time
from collections.abc import Callable, Iterator, Mapping
from importlib.metadata import version

import pytest

from kora.conformance.capture import WAIT_SECONDS, Pipe
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
from tests.support.observability import FakeClock, ProcessResult, run_python

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
KORA_VERSION_LINE: bytes = f"kora {version('kora')}\n".encode()


def run_kora(arguments: list[str], environment: Mapping[str, str]) -> ProcessResult:
    return run_python(["-m", "kora", *arguments], environment)


@pytest.fixture
def pipe() -> Iterator[Pipe]:
    created = Pipe.open()
    yield created
    created.release()


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


def configured_with_reader(
    configure: Configure, pipe: Pipe, settings: LoggingSettings
) -> ProcessDiagnostics:
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()
    return diagnostics


def finished_output(diagnostics: ProcessDiagnostics, pipe: Pipe) -> str:
    diagnostics.shutdown()
    pipe.close_write_end()
    pipe.reader.wait_closed()
    return "\n".join(pipe.reader.lines())


def wait_until(
    condition: Callable[[], bool], deadline_seconds: float = WAIT_SECONDS
) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("the condition was not met before the deadline")
        time.sleep(0.005)
