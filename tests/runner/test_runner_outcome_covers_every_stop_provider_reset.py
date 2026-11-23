from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.ir import ErrorClass
from kora.ir.json import JsonObject
from kora.protocols.runtime import SessionResultObservation
from kora.runner.agent import AgentHost
from tests.runner.agent_support import (
    FIXED_NOW,
    PAUSE,
    SteppedClock,
    make_world,
    script,
)


def observed(host: AgentHost) -> SessionResultObservation:
    observation = host.observation()
    assert isinstance(observation, SessionResultObservation)
    return observation


@pytest.mark.parametrize("reset", [None, 60, 61])
@pytest.mark.asyncio
async def test_runner_outcome_covers_every_stop_provider_reset(
    tmp_path: Path, kora_capture: Capture, reset: int | None
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    event: JsonObject = {
        "kind": "event",
        "event": "provider_limited",
        "data": {
            "retry_after": None
            if reset is None
            else (FIXED_NOW + timedelta(seconds=reset)).isoformat(),
            "scope": "rate",
            "vendor_group": "group",
            "model": "model",
            "binding": "binding",
        },
    }
    source = script([event, PAUSE])
    host = world.host(world.resolver(source), clock=clock)
    running = False

    try:
        host.start(
            world.workload(limits={"stall_after_us": "60000000"}), world.workspace()
        )
        if reset is None or reset <= 60:
            await source.paused.wait()
            running = host.observation() is None
            await host.cancel()
        await host.finished()
        observation = observed(host)
    finally:
        await host.stop()
        world.close()

    assert observation.cause == ("provider_limit" if reset == 61 else "cancel")
    assert running is (reset != 61)
    assert observation.result.error_class is (
        ErrorClass.PROVIDER_LIMITED if reset == 61 else ErrorClass.CANCELLED
    )
