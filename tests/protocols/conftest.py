import os
from collections.abc import Callable, Iterator

import pytest

from kora.observability import (
    CATALOG,
    LoggingSettings,
    ProcessDiagnostics,
    configure_process,
    current_diagnostics,
)
from tests.support.observability import FakeClock, Pipe, PipeReader

TEST_COMPONENT: str = CATALOG.register_component("testing")
INSTANCE_ID: str = "protocols-test"
DRAIN_SECONDS: float = 5.0


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
        diagnostics.shutdown(DRAIN_SECONDS)
    assert current_diagnostics() is None
