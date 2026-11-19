import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.ir import ErrorClass
from kora.ir.json import JsonObject
from kora.plugins.harness.fake.script import Step
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.runtime import SessionResultObservation
from kora.runner.agent import AgentHost
from kora.runner.journal import SYNC_DEADLINE_NS
from kora.runner.liveness import WRITER_STALL_NS
from kora.runner.sink import QUEUE_RECORDS
from tests.plugins.harness.fake.fake_support import turn
from tests.runner.agent_support import (
    FIXED_NOW,
    PAUSE,
    HeldWriter,
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
