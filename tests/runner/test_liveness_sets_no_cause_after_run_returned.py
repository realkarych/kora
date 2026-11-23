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
async def test_liveness_sets_no_cause_after_run_returned(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    sink = RunnerSink(world.journal, SecretRegistry(), clock, None, STALL)

    try:
        await clock.advance_to(STALL + SECOND)
        await watch_liveness(sink, clock, STALL, None, lambda: True)
        cause = sink.cause()
    finally:
        await drained(sink)
        world.close()

    assert cause is None
