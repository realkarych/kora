import asyncio
import sys
from types import CoroutineType, FrameType
from typing import cast

import pytest

from kora.protocols.time import SystemClock


@pytest.mark.asyncio
async def test_large_finite_phase_sum_wait_releases_its_actual_timer() -> None:
    clock = SystemClock()
    duration = int(sys.float_info.max) * 1_000_000_000
    target = clock.monotonic_ns() + duration * 4
    sleeper = clock.sleep_until(target)
    task = asyncio.create_task(sleeper)
    loop = asyncio.get_running_loop()
    entered: asyncio.Future[None] = loop.create_future()
    loop.call_soon(entered.set_result, None)
    handles: list[asyncio.TimerHandle] = []
    waits: list[asyncio.Future[object]] = []

    try:
        await asyncio.wait_for(entered, 5)
        if task.done():
            await task
        assert not task.done()
        current: object = sleeper
        frame: FrameType | None = None
        value: object = None
        while isinstance(current, CoroutineType):
            frame = current.cr_frame
            if frame is not None:
                for value in frame.f_locals.values():
                    if isinstance(value, asyncio.TimerHandle):
                        handles.append(value)
                    if isinstance(value, asyncio.Future):
                        waits.append(cast(asyncio.Future[object], value))
            current = current.cr_await
        del current
        del frame
        del value
        assert len(handles) == 1
        assert len(waits) == 1
        assert not handles[0].cancelled()
        assert not waits[0].done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert sleeper.cr_frame is None
    assert handles[0].cancelled()
    assert waits[0].cancelled()
    assert clock.monotonic_ns() < target


@pytest.mark.asyncio
async def test_expired_system_clock_target_completes_without_suspending() -> None:
    clock = SystemClock()
    target = clock.monotonic_ns()
    sleeper = clock.sleep_until(target)

    with pytest.raises(StopIteration):
        sleeper.send(None)

    assert sleeper.cr_frame is None


@pytest.mark.asyncio
async def test_positive_system_clock_wait_reaches_its_original_target() -> None:
    clock = SystemClock()
    target = clock.monotonic_ns() + 1_000_000

    await asyncio.wait_for(clock.sleep_until(target), 5)

    assert clock.monotonic_ns() >= target
