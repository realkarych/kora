from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import ErrorClass
from kora.plugins.harness.fake.script import Step
from kora.protocols.deadlines import ExecutionDeadline
from kora.runner.session_result import RESULT_DIRECTORY, RESULT_NAME
from tests.plugins.harness.fake.fake_support import tool_call
from tests.runner.agent_support import (
    FIXED_NOW,
    PAUSE,
    FakeResolver,
    ObservedFakeHarness,
    SignalledScript,
    SteppedClock,
    make_world,
    script,
)

SECOND: Final[int] = 1_000_000_000
STALL: Final[int] = 60 * SECOND


def deadline(seconds: int, uncertainty: int = 0) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=FIXED_NOW + timedelta(seconds=seconds),
        scope_kind="run",
        scope_id="run-1",
        clock_uncertainty=timedelta(seconds=uncertainty),
        grace=timedelta(seconds=30),
    )


class TimerStartedScript(SignalledScript):
    def __init__(self, source: SignalledScript, clock: SteppedClock) -> None:
        super().__init__(source.header, source.listed)
        self.clock = clock

    async def steps(self) -> AsyncGenerator[Step]:
        await self.clock.requested(STALL)
        await self.clock.advance_to(30 * SECOND)
        async for item in super().steps():
            yield item


@pytest.mark.asyncio
async def test_runner_outcome_covers_every_stop_open_call(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    source = TimerStartedScript(
        script([tool_call("Bash", "c1", outcome=None), PAUSE]), clock
    )
    resolver = FakeResolver(
        source, context_for(kora_capture), adapter_type=ObservedFakeHarness
    )
    host = world.host(resolver, clock=clock)
    work = world.workload(limits={"stall_after_us": "60000000"}, deadline=deadline(120))

    try:
        host.start(work, world.workspace())
        await source.paused.wait()
        await clock.advance_to(STALL + SECOND)
        await clock.requested(120 * SECOND)
        adapter = resolver.adapters[0]
        assert isinstance(adapter, ObservedFakeHarness)
        sink = adapter.runner_sink
        assert sink is not None
        open_before_deadline = sink.liveness().calls_open
        stopped_before_deadline = sink.cause()
        killed_before_deadline = adapter.killed.is_set()
        running = adapter.finalized_result is None and host.result_reference() is None
        await clock.advance_to(120 * SECOND)
        await host.finished()
    finally:
        released = await host.stop()
        world.close()

    assert running
    assert open_before_deadline
    assert stopped_before_deadline is None
    assert not killed_before_deadline
    assert sink.cause() == "deadline"
    assert adapter.finalized_result is not None
    assert adapter.finalized_result.stop_reason == "deadline"
    assert adapter.finalized_result.error_class is ErrorClass.TIMEOUT
    assert host.result_reference() is None
    assert not (world.root / "control" / RESULT_DIRECTORY / RESULT_NAME).exists()
    assert "runner.session_result" not in world.kinds()
    assert released.session
    assert released.writer
