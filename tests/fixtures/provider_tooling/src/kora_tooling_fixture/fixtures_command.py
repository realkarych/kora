import asyncio
import socket
from collections.abc import Generator

import pytest

from kora.conformance.bridge import PreparedConformance
from kora.ir.json import JsonObject
from kora.protocols.lifetime import (
    ConformanceLifetime,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora_tooling_fixture.configured_adapter import Adapter


class CommandAdapter(Adapter):
    feedback: str | None = None

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        mode = fixture_data.get("mode", "normal")
        assert isinstance(mode, str)
        self.mode = mode
        feedback = fixture_data.get("feedback")
        assert feedback is None or isinstance(feedback, str)
        self.feedback = feedback
        prepared = await super().prepare(config, fixture_data, lifetime)
        if mode in {"cancel", "shutdown", "crash", "idle"}:
            tree = await lifetime.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.TEMPORARY_TREE,
                )
            )
            acquired = await lifetime.acquire(
                tree.token, new_operation_id(), TemporaryTreeRequest()
            )
            await lifetime.acknowledge(
                tree.token, acquired.operation_id, acquired.revision
            )
            feedback = fixture_data["feedback"]
            assert isinstance(feedback, str)
            loop = asyncio.get_running_loop()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
                endpoint.setblocking(False)
                await loop.sock_connect(endpoint, feedback)
                await loop.sock_sendall(endpoint, tree.token.invocation_id.encode())
                assert await loop.sock_recv(endpoint, 1) == b"a"
            if mode == "shutdown":
                await lifetime.close(None)
        return prepared


@pytest.fixture
def kora_conformance_adapter() -> Generator[CommandAdapter]:
    adapter = CommandAdapter()

    yield adapter

    if adapter.mode == "delayed_teardown":
        assert adapter.feedback is not None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
            endpoint.connect(adapter.feedback)
            endpoint.sendall(b"teardown")
            assert endpoint.recv(1) == b"a"
    if adapter.mode in {"teardown", "delayed_teardown"}:
        raise ValueError("private-command-teardown-marker")
    if adapter.mode != "prepare":
        assert adapter.cleaned
