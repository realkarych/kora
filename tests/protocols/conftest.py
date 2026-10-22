from collections.abc import Callable, Iterator
from contextlib import ExitStack

import pytest

from kora.conformance.capture import Capture, captured
from kora.observability import CATALOG, LoggingSettings
from tests.support.observability import FakeClock

TEST_COMPONENT: str = CATALOG.register_component("testing")
INSTANCE_ID: str = "protocols-test"
DRAIN_SECONDS: float = 5.0

StartCapture = Callable[[LoggingSettings], Capture]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def start_capture(clock: FakeClock) -> Iterator[StartCapture]:
    with ExitStack() as stack:

        def start(settings: LoggingSettings) -> Capture:
            return stack.enter_context(
                captured(
                    TEST_COMPONENT, settings, INSTANCE_ID, clock.now, DRAIN_SECONDS
                )
            )

        yield start
