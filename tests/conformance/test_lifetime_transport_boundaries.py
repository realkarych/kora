import asyncio
import socket
import sys
from pathlib import Path

import pytest

from kora.conformance.controllers import duration_ns
from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.conformance.transport import (
    CALL,
    ENVELOPE,
    HEADER,
    CloseHeader,
    CloseReply,
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
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock


@pytest.mark.asyncio
@pytest.mark.parametrize("late_ns", [0, 1])
async def test_client_rejects_close_reply_at_the_original_deadline(
    tmp_path: Path, late_ns: int
) -> None:
    left, right = socket.socketpair()
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
    client = LifetimeClient(left, clock=clock)
    actual = SystemClock()
    peer = BoundedChannel(right, actual)
    request = ReservationRequest(
        operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
    )
    reserving = None
    closing = None
    deadline = actual.monotonic_ns() + 5_000_000_000
    try:
        async with asyncio.timeout(5):
            reservation = await owner.reserve(request)
            reserving = asyncio.create_task(client.reserve(request))
            header = HEADER.validate_json(await peer.read_frame(deadline, 256))
            assert isinstance(header, OrdinaryHeader)
            await peer.read_frame(deadline, header.control_bytes)
            await peer.write_record(
                ENVELOPE.dump_json(
                    OrdinaryReply(
                        request_id=header.request_id,
                        reply=SuccessReply(value=reservation),
                    )
                ),
                deadline,
            )
            assert await reserving == reservation
            closing = asyncio.create_task(client.close(reservation.token.resource_id))
            assert isinstance(
                HEADER.validate_json(await peer.read_frame(deadline, 256)), CloseHeader
            )
            report = await owner.close(reservation.token.resource_id)
            await peer.write_record(
                ENVELOPE.dump_json(
                    CloseReply(resource_id=reservation.token.resource_id, report=report)
                ),
                deadline,
            )
            clock.advance(
                sum(
                    duration_ns(seconds)
                    for seconds in (
                        client.limits.cooperative_seconds,
                        client.limits.term_seconds,
                        client.limits.kill_observation_seconds,
                        client.limits.drain_seconds,
                    )
                )
                + late_ns
            )

            with pytest.raises(LifetimeTransportError) as refused:
                await asyncio.wait_for(asyncio.shield(closing), 5)

            assert refused.value.reason is ChannelFailure.DEADLINE
            assert client.closed
            assert not client.resources
    finally:
        client.disconnect()
        peer.close()
        if reserving is not None:
            reserving.cancel()
        if closing is not None:
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        if reserving is not None:
            await asyncio.gather(reserving, return_exceptions=True)
        try:
            await asyncio.wait_for(asyncio.gather(client.reader, client.writer), 5)
        finally:
            await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
async def test_client_idle_time_does_not_consume_the_next_reply_budget() -> None:
    left, right = socket.socketpair()
    clock = ControlledLifetimeClock()
    client = LifetimeClient(left, clock=clock)
    peer = BoundedChannel(right, SystemClock())
    entered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(entered.set_result, None)

    async def reply() -> None:
        deadline = SystemClock().monotonic_ns() + 5_000_000_000
        header = HEADER.validate_json(await peer.read_frame(deadline, 256))
        assert isinstance(header, OrdinaryHeader)
        call = CALL.validate_json(await peer.read_frame(deadline, header.control_bytes))
        assert call.verb == "observe"
        await peer.write_record(
            ENVELOPE.dump_json(
                OrdinaryReply(
                    request_id=header.request_id, reply=SuccessReply(value=())
                )
            ),
            deadline,
        )

    response: asyncio.Task[None] | None = None
    try:
        await entered
        clock.advance(60_000_000_000)
        response = asyncio.create_task(reply())
        observed = await asyncio.wait_for(client.observe(None), 5)
        await response

        assert observed == ()
        assert not client.closed
        assert not client.pending
    finally:
        client.disconnect()
        peer.close()
        if response is not None:
            response.cancel()
            await asyncio.gather(response, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(client.reader, client.writer), 5)

    assert not clock.waiters
    assert client.control_bytes == 0
    assert client.execution_bytes == 0


@pytest.mark.asyncio
async def test_client_partial_reply_keeps_the_budget_started_after_idle() -> None:
    left, right = socket.socketpair()
    clock = ControlledLifetimeClock()
    actual = SystemClock()
    client = LifetimeClient(left, clock=clock)
    peer = BoundedChannel(right, actual)
    entered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(entered.set_result, None)
    waiting = None
    deadline = actual.monotonic_ns() + 5_000_000_000
    try:
        await entered
        clock.advance(29_000_000_000)
        request_deadline = clock.monotonic_ns() + 30_000_000_000
        waiting = asyncio.create_task(client.observe(None))
        header = HEADER.validate_json(await peer.read_frame(deadline, 256))
        assert isinstance(header, OrdinaryHeader)
        await peer.read_frame(deadline, header.control_bytes)
        encoded = ENVELOPE.dump_json(
            OrdinaryReply(request_id=header.request_id, reply=SuccessReply(value=()))
        )
        wire = (
            (4).to_bytes(4, "big")
            + len(encoded).to_bytes(4, "big")
            + len(encoded).to_bytes(4, "big")
            + encoded
        )
        assert right.send(wire[:2]) == 2
        while len(clock.waiters) < 3:
            assert actual.monotonic_ns() < deadline
            progress: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(progress.set_result, None)
            await progress
        assert set(clock.waiters.values()) == {request_deadline}

        clock.advance(2_000_000_000)
        assert right.send(wire[2:]) == len(wire) - 2
        observed = await asyncio.wait_for(waiting, 5)

        assert observed == ()
        assert not client.closed
        assert not client.pending
    finally:
        client.disconnect()
        peer.close()
        if waiting is not None:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(client.reader, client.writer), 5)

    assert not clock.waiters


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["ordinary", "close"])
async def test_idle_reader_does_not_extend_outstanding_work_deadlines(
    verb: str,
) -> None:
    left, right = socket.socketpair()
    clock = ControlledLifetimeClock()
    actual = SystemClock()
    client = LifetimeClient(left, clock=clock)
    peer = BoundedChannel(right, actual)
    waiting = asyncio.create_task(
        client.close(None) if verb == "close" else client.observe(None)
    )
    deadline = actual.monotonic_ns() + 5_000_000_000
    try:
        header = HEADER.validate_json(await peer.read_frame(deadline, 256))
        if isinstance(header, OrdinaryHeader):
            await peer.read_frame(deadline, header.control_bytes)
        clock.advance((14 if verb == "close" else 30) * 1_000_000_000)

        with pytest.raises(LifetimeTransportError) as refused:
            await asyncio.wait_for(waiting, 5)

        assert refused.value.reason is ChannelFailure.DEADLINE
        assert client.closed
        assert not client.pending
        assert not client.resources
    finally:
        client.disconnect()
        peer.close()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(client.reader, client.writer), 5)

    assert not clock.waiters


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
