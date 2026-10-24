import asyncio
import socket
import sys

import pytest

from kora.conformance.lifetime import LifetimeError
from kora.conformance.transport import (
    CALL,
    ENVELOPE,
    HEADER,
    CloseHeader,
    FailureReply,
    LifetimeClient,
    LifetimeTransportError,
    OrdinaryHeader,
    OrdinaryReply,
    SuccessReply,
)
from kora.protocols.lifetime import (
    LifetimeLimits,
    LifetimeReason,
    ReservationRequest,
    ResourceKind,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel, ChannelFailure


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["wrong_verb", "unknown_id", "replayed_id", "out_of_order", "malformed"]
)
async def test_client_replies_validate_correlation_and_original_verb(mode: str) -> None:
    left, right = socket.socketpair()
    clock = SystemClock()
    client = LifetimeClient(left)
    peer = BoundedChannel(right, clock)
    deadline = clock.monotonic_ns() + 5_000_000_000
    waiting: list[asyncio.Task[object]] = []

    async def invoke() -> object:
        if mode == "wrong_verb":
            return await client.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
        return await client.observe(None)

    try:
        headers: list[OrdinaryHeader] = []
        for _ in range(2 if mode == "out_of_order" else 1):
            waiting.append(asyncio.create_task(invoke()))
            header = HEADER.validate_json(await peer.read_frame(deadline, 256))
            assert isinstance(header, OrdinaryHeader)
            call = CALL.validate_json(
                await peer.read_frame(deadline, header.control_bytes)
            )
            assert call.verb == header.verb
            headers.append(header)
        reasons = (LifetimeReason.CAPACITY_EXCEEDED, LifetimeReason.IDENTITY_UNKNOWN)
        replies = [
            OrdinaryReply(
                request_id=header.request_id + int(mode == "unknown_id"),
                reply=SuccessReply(value=())
                if mode == "wrong_verb"
                else FailureReply(reason=reasons[index]),
            )
            for index, header in enumerate(headers)
        ]

        for reply in reversed(replies):
            await peer.write_record(
                b"{" if mode == "malformed" else ENVELOPE.dump_json(reply), deadline
            )
        if mode in ("out_of_order", "replayed_id"):
            for index, task in enumerate(waiting):
                with pytest.raises(LifetimeError) as failed:
                    await asyncio.wait_for(asyncio.shield(task), 5)
                assert failed.value.reason is reasons[index]
            assert not client.pending
            assert not client.closed
            if mode == "replayed_id":
                await peer.write_record(ENVELOPE.dump_json(replies[0]), deadline)
                await asyncio.wait_for(client.reader, 5)
                assert client.closed
        else:
            with pytest.raises(LifetimeTransportError) as refused:
                await asyncio.wait_for(asyncio.shield(waiting[0]), 5)
            assert refused.value.reason is ChannelFailure.MALFORMED
            assert client.closed
        assert not client.pending
    finally:
        client.disconnect()
        peer.close()
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(client.reader, client.writer), 5)

    assert client.control_bytes == 0
    assert client.execution_bytes == 0
    assert not client.resources
