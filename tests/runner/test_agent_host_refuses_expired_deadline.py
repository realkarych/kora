from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from tests.runner.agent_support import (
    FIXED_NOW,
    PAUSE,
    SteppedClock,
    make_world,
    script,
)

SECOND: Final[int] = 1_000_000_000


def deadline(seconds: int, uncertainty: int = 0) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=FIXED_NOW + timedelta(seconds=seconds),
        scope_kind="run",
        scope_id="run-1",
        clock_uncertainty=timedelta(seconds=uncertainty),
        grace=timedelta(seconds=30),
    )


@pytest.mark.parametrize(("at", "uncertainty"), [(0, 0), (10, 10), (-1, 0)])
@pytest.mark.asyncio
async def test_agent_host_refuses_expired_deadline(
    tmp_path: Path, kora_capture: Capture, at: int, uncertainty: int
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    resolver = world.resolver(script([PAUSE]))
    host = world.host(resolver, clock=clock)

    try:
        with pytest.raises(KoraProtocolError) as raised:
            host.start(
                world.workload(deadline=deadline(at, uncertainty)), world.workspace()
            )
    finally:
        await host.stop()
        world.close()

    assert raised.value.code == "deadline_expired"
    assert not raised.value.retryable
    assert world.records() == []
    assert resolver.adapters == []
