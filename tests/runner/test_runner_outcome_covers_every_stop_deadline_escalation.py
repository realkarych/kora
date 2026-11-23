import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import ErrorClass
from kora.protocols.deadlines import ExecutionDeadline
from kora.runner.session_result import RESULT_DIRECTORY, RESULT_NAME
from tests.runner.agent_support import (
    FIXED_NOW,
    PAUSE,
    FakeResolver,
    ObservedFakeHarness,
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


@pytest.mark.parametrize(
    ("cancel_at", "observed_at", "kill_seconds"),
    [(None, 120, 150), (110, 110, 140), (None, 130, 150), (None, 160, 160)],
)
@pytest.mark.asyncio
async def test_runner_outcome_covers_every_stop_deadline_escalation(
    tmp_path: Path,
    kora_capture: Capture,
    cancel_at: int | None,
    observed_at: int,
    kill_seconds: int,
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    source = script([PAUSE], on_interrupt="ignore")
    resolver = FakeResolver(
        source, context_for(kora_capture), adapter_type=ObservedFakeHarness
    )
    host = world.host(resolver, clock=clock)
    kill_at = kill_seconds * SECOND

    try:
        host.start(world.workload(deadline=deadline(120)), world.workspace())
        await source.paused.wait()
        await clock.requested(120 * SECOND)
        await clock.advance_to(observed_at * SECOND)
        stopped = asyncio.create_task(
            host.finished() if cancel_at is None else host.cancel()
        )
        before_kill = resolver.adapters[0].killed.is_set()
        if observed_at < kill_seconds:
            await clock.requested(kill_at)
            before_kill = resolver.adapters[0].killed.is_set()
            await clock.advance_to(kill_at)
        await resolver.adapters[0].killed.wait()
        await stopped
        adapter = resolver.adapters[0]
        requested = list(clock.deadlines)
    finally:
        released = await host.stop()
        world.close()
    observations = kora_capture.finish()
    causes = [
        record["cause"]
        for record in observations
        if record.get("event") == "harness.fake.session_finished"
    ]

    assert not before_kill
    if observed_at > 120:
        assert (observed_at + 30) * SECOND not in requested
    expected_cause = "deadline" if cancel_at is None else "cancel"
    assert causes == [expected_cause]
    assert isinstance(adapter, ObservedFakeHarness)
    assert adapter.runner_sink is not None
    assert adapter.runner_sink.cause() == expected_cause
    assert adapter.finalized_outcome is not None
    assert adapter.finalized_outcome.cause == expected_cause
    assert adapter.finalized_result is not None
    assert adapter.finalized_result.stop_reason == expected_cause
    assert adapter.finalized_result.error_class is (
        ErrorClass.TIMEOUT if cancel_at is None else ErrorClass.CANCELLED
    )
    assert released.session
    assert released.writer
    assert host.result_reference() is None
    assert not (world.root / "control" / RESULT_DIRECTORY / RESULT_NAME).exists()
    assert "runner.session_result" not in world.kinds()
