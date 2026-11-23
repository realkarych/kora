import asyncio
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.observability.redaction import SecretRegistry
from kora.runner.liveness import watch_liveness
from kora.runner.sink import RunnerSink
from tests.runner.agent_support import (
    SteppedClock,
    drained,
    make_world,
)

SECOND: Final[int] = 1_000_000_000
STALL: Final[int] = 60 * SECOND


@pytest.mark.asyncio
async def test_internal_retry_is_liveness_not_stall(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    sink = RunnerSink(world.journal, SecretRegistry(), clock, None, STALL)
    timer = asyncio.create_task(watch_liveness(sink, clock, STALL, None, lambda: False))
    alive: list[bool] = []

    try:
        await clock.requested(STALL)
        for retry in range(1, 4):
            at = retry * 40 * SECOND
            await clock.advance_to(at)
            sink.emit("provider_retrying", {"model": "model", "retry_number": retry})
            await clock.advance_to(at + 20 * SECOND)
            await clock.requested(at + STALL)
            alive.append(sink.cause() is None)
        await clock.advance_to(180 * SECOND)
        cause = await sink.termination()
        await timer
        requested = list(clock.deadlines)
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        await drained(sink)
        world.close()

    assert alive == [True, True, True]
    assert cause == "stall"
    assert requested == [60 * SECOND, 100 * SECOND, 140 * SECOND, 180 * SECOND]
