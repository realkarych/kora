import logging
import os
import socket
import warnings
from collections.abc import Generator

import pytest

from kora_tooling_fixture.configured_adapter import Adapter

PAYLOAD: bytes = b"private-installed-output-marker" * 65536


def noise(phase: bytes) -> None:
    for descriptor in (1, 2):
        remaining = PAYLOAD
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
    logging.getLogger("fixture.noisy").warning(PAYLOAD.decode())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.connect(os.environ["KORA_FIXTURE_FEEDBACK"])
        endpoint.sendall(phase)
        assert endpoint.recv(1) == b"a"


@pytest.fixture
def kora_conformance_adapter() -> Generator[Adapter]:
    if os.environ["KORA_FIXTURE_MODE"] == "warning":
        warnings.warn("private-installed-warning-marker", UserWarning, stacklevel=1)
    noise(b"s")
    adapter = Adapter(exercise=lambda: noise(b"c"))

    yield adapter

    assert adapter.cleaned
    noise(b"t")
