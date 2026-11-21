import asyncio
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
from kora.protocols.runtime import SessionResultObservation
from kora.runner.agent import AgentHost
from kora.runner.journal import SYNC_DEADLINE_NS
from kora.runner.liveness import WRITER_STALL_NS
from kora.runner.session_result import RESULT_DIRECTORY, RESULT_NAME
from kora.runner.sink import QUEUE_RECORDS
from tests.plugins.harness.fake.fake_support import turn
from tests.runner.agent_support import (
    FIXED_NOW,
    PAUSE,
    FakeResolver,
    HeldWriter,
    ObservedFakeHarness,
    SignalledScript,
    SteppedClock,
    advance_batch,
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


class WriterHeldScript(SignalledScript):
    def __init__(
        self, writer: HeldWriter, clock: SteppedClock, repeat_refusals: bool
    ) -> None:
        source = script([turn(f"m{i}") for i in range(QUEUE_RECORDS + 2)] + [PAUSE])
        super().__init__(source.header, source.listed)
        self.writer = writer
        self.clock = clock
        self.repeat_refusals = repeat_refusals

    async def steps(self) -> AsyncGenerator[Step]:
        yield self.listed[0]
        await advance_batch(self.clock)
        assert await asyncio.to_thread(self.writer.entered.wait, 10)
        await self.clock.advance_to(5 * SECOND)
        for item in self.listed[1:-1]:
            yield item
        await self.clock.requested(35 * SECOND)
        if self.repeat_refusals:
            await self.clock.advance_to(12 * SECOND)
            yield self.listed[-2]
            yield self.listed[-2]
        self.paused.set()
        yield self.listed[-1]


async def stuck_writer(
    tmp_path: Path, capture: Capture, stall_seconds: int
) -> SessionResultObservation:
    world = make_world(tmp_path, capture)
    clock = SteppedClock()
    writer = HeldWriter(journal=world.journal)
    source = WriterHeldScript(writer, clock, repeat_refusals=stall_seconds > 30)
    host = world.host(world.resolver(source), journal=writer, clock=clock)
    try:
        host.start(
            world.workload(limits={"stall_after_us": str(stall_seconds * 1_000_000)}),
            world.workspace(),
        )
        await source.paused.wait()
        writer_due = 5 * SECOND + WRITER_STALL_NS
        await clock.requested(writer_due)
        await clock.advance_to(writer_due)
        await clock.requested(writer_due + SYNC_DEADLINE_NS)
        assert 42 * SECOND not in clock.deadlines
        writer.gate.set()
        await host.finished()
        return observed(host)
    finally:
        writer.gate.set()
        await host.stop()
        world.close()


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
