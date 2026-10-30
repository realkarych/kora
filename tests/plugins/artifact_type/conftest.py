from collections.abc import Generator

import pytest

from kora.conformance.artifact_type import artifact_type_capture_settings
from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now


@pytest.fixture
def kora_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT,
        artifact_type_capture_settings(),
        "artifact-type",
        utc_now,
    ) as capture:
        yield capture
