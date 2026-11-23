from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.ir import ErrorClass
from kora.plugins.harness.fake.script import Step
from kora.protocols.runtime import SessionResultObservation
from kora.runner.agent import AgentHost
from tests.plugins.harness.fake.fake_support import tool_call, turn
from tests.runner.agent_support import (
    PAUSE,
    SignalledScript,
    SteppedClock,
    make_world,
    script,
)

SECOND: Final[int] = 1_000_000_000
STALL: Final[int] = 60 * SECOND


def observed(host: AgentHost) -> SessionResultObservation:
    observation = host.observation()
    assert isinstance(observation, SessionResultObservation)
    return observation


class TimerStartedScript(SignalledScript):
    def __init__(self, source: SignalledScript, clock: SteppedClock) -> None:
        super().__init__(source.header, source.listed)
        self.clock = clock

    async def steps(self) -> AsyncGenerator[Step]:
        await self.clock.requested(STALL)
        await self.clock.advance_to(30 * SECOND)
        async for item in super().steps():
            yield item


@pytest.mark.parametrize("stall_seconds", [0, 60])
@pytest.mark.asyncio
async def test_runner_outcome_covers_every_stop_stall(
    tmp_path: Path, kora_capture: Capture, stall_seconds: int
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    source = script([turn("m1"), tool_call("Bash", "c1"), PAUSE])
    if stall_seconds:
        source = TimerStartedScript(source, clock)
    host = world.host(world.resolver(source), clock=clock)

    try:
        host.start(
            world.workload(limits={"stall_after_us": str(stall_seconds * 1_000_000)}),
            world.workspace(),
        )
        running = host.observation() is None
        if stall_seconds:
            await source.paused.wait()
            await clock.advance_to(STALL)
            await clock.requested(90 * SECOND)
            running = host.observation() is None
            await clock.advance_to(90 * SECOND)
        await host.finished()
        observation = observed(host)
    finally:
        await host.stop()
        world.close()

    assert running
    assert observation.cause == "stall"
    assert observation.result.error_class is ErrorClass.STALLED
