import asyncio
import errno
import os
import signal

import pytest

from kora.protocols.time import SystemClock
from kora.runner.channels import CaptureDrain, ChannelError, ChannelFailure
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import (
    ProcessCondition,
    ProcessError,
    RecoveredProcess,
    SignalResult,
)
from tests.conformance.test_recovered_process_lifetime import domain


@pytest.mark.asyncio
async def test_done_recovery_readiness_does_not_establish_unknown_domain_closure() -> (
    None
):
    clock = SystemClock()
    native = NativeIdentityReader(clock=clock)
    async with domain("normal") as fixture:
        owner = RecoveredProcess(fixture.registration, clock, native)
        deadline = clock.monotonic_ns() + 5_000_000_000
        try:
            before = owner.observe(deadline)
            pending = owner.readiness()
            assert before.condition is ProcessCondition.LIVE
            assert pending
            assert all(not future.done() for future in pending)

            owner.dispose_observation()
            observed = owner.observe(deadline)
            with pytest.raises(ProcessError) as unsettled:
                await owner.settle(deadline)
            with pytest.raises(ProcessError) as unreleased:
                owner.release(deadline)

            assert all(future.cancelled() for future in pending)
            assert owner.readiness() == ()
            assert observed.condition is ProcessCondition.UNKNOWN
            assert unsettled.value.condition is ProcessCondition.UNKNOWN
            assert unreleased.value.condition is ProcessCondition.UNKNOWN
            assert not owner.closed
            assert owner.signal(signal.SIGKILL, deadline) is SignalResult.UNKNOWN
            live = native.read(fixture.registration.process.pid).process
            assert live is not None
            assert live.identity == fixture.registration.process
            assert not live.dead
        finally:
            owner.dispose_observation()


@pytest.mark.asyncio
async def test_closed_capture_readiness_is_not_observed_eof() -> None:
    clock = SystemClock()
    reading, writing = os.pipe()
    capture = CaptureDrain(reading)
    deadline = clock.monotonic_ns() + 5_000_000_000
    waiting = asyncio.create_task(capture.drain(deadline, clock))
    try:
        os.write(writing, b"x")
        while capture.bytes_discarded == 0:
            assert clock.monotonic_ns() < deadline
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(ready.set_result, None)
            await ready
        assert not capture.readiness.done()
        assert not capture.eof

        capture.close()
        with pytest.raises(ChannelError) as failed:
            await waiting

        assert failed.value.reason is ChannelFailure.CLOSED
        assert capture.readiness.done()
        assert not capture.readiness.cancelled()
        assert capture.closed
        assert not capture.eof
        with pytest.raises(OSError, match="Bad file descriptor") as closed_descriptor:
            os.fstat(reading)
        assert closed_descriptor.value.errno == errno.EBADF
    finally:
        capture.close()
        os.close(writing)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
