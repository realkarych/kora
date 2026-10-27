import asyncio
import atexit
import socket
from collections.abc import Generator

import pytest

from kora.conformance.bridge import PreparedConformance
from kora.ir.json import JsonObject
from kora.protocols.lifetime import ConformanceLifetime
from kora_tooling_fixture.configured_adapter import Adapter
from kora_tooling_fixture.measurement import counters, final_counters


class MeasuredAdapter(Adapter):
    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        mode = fixture_data["mode"]
        feedback = fixture_data["feedback"]
        assert isinstance(mode, str)
        assert isinstance(feedback, str)
        self.mode = mode
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.setblocking(False)
        atexit.register(final_counters, endpoint)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(endpoint, feedback)
        await loop.sock_sendall(endpoint, counters("barrier"))
        assert await loop.sock_recv(endpoint, 1) == b"a"
        endpoint.setblocking(True)
        if mode == "shutdown":
            await lifetime.close(None)
        return await super().prepare(config, fixture_data, lifetime)


@pytest.fixture
def kora_conformance_adapter() -> Generator[MeasuredAdapter]:
    adapter = MeasuredAdapter()

    yield adapter

    if adapter.mode not in {"prepare", "shutdown"}:
        assert adapter.cleaned
