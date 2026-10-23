import asyncio
import socket
import sys

import pytest

from kora.conformance.transport import (
    HEADER,
    CloseHeader,
    LifetimeClient,
    OrdinaryHeader,
)
from kora.protocols.lifetime import LifetimeLimits
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "close"])
async def test_large_finite_client_deadlines_preserve_owned_waits_until_disconnect(
    mode: str,
) -> None:
    left, right = socket.socketpair()
    clock = SystemClock()
    client = LifetimeClient(
        left,
        limits=LifetimeLimits(
            acquisition_seconds=sys.float_info.max,
            cooperative_seconds=sys.float_info.max,
            term_seconds=sys.float_info.max,
            kill_observation_seconds=sys.float_info.max,
            drain_seconds=sys.float_info.max,
        ),
    )
    peer = BoundedChannel(right, clock)
    waiting = asyncio.create_task(
        client.close(None) if mode == "close" else client.observe(None)
    )
    deadline = clock.monotonic_ns() + 5_000_000_000
    try:
        header = HEADER.validate_json(await peer.read_frame(deadline, 256))
        if mode == "ordinary":
            assert isinstance(header, OrdinaryHeader)
            await peer.read_frame(deadline, header.control_bytes)
            assert len(client.pending) == 1
        else:
            assert isinstance(header, CloseHeader)
            assert client.resources[None].deadline > int(sys.float_info.max)
        assert not waiting.done()
        assert not client.closed

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not client.closed
        client.disconnect()
        await asyncio.wait_for(asyncio.gather(client.reader, client.writer), 5)

        assert client.closed
        assert not client.pending
        assert not client.resources
        assert client.control_bytes == 0
        assert client.execution_bytes == 0
        assert client.reader.done()
        assert client.writer.done()
    finally:
        client.disconnect()
        peer.close()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(client.reader, client.writer), 5)
