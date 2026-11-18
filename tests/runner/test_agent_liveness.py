import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.ir import ErrorClass
from kora.ir.json import JsonObject
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.fake.script import Step
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import SessionResultObservation
from kora.runner.agent import AgentHost
from kora.runner.journal import SYNC_DEADLINE_NS
from kora.runner.liveness import WRITER_STALL_NS, watch_liveness
from kora.runner.sink import QUEUE_RECORDS, RunnerSink
from tests.plugins.harness.fake.fake_support import tool_call, turn
from tests.runner.agent_support import (
    FIXED_NOW,
    PAUSE,
    HeldWriter,
    SignalledScript,
    SteppedClock,
    drained,
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


@pytest.mark.asyncio
async def test_runner_outcome_covers_every_stop_open_call(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    source = TimerStartedScript(
        script([tool_call("Bash", "c1", outcome=None), PAUSE]), clock
    )
    host = world.host(world.resolver(source), clock=clock)
    work = world.workload(limits={"stall_after_us": "60000000"}, deadline=deadline(120))

    try:
        host.start(work, world.workspace())
        await source.paused.wait()
        await clock.advance_to(STALL + SECOND)
        await clock.requested(120 * SECOND)
        running = host.observation() is None
        await clock.advance_to(120 * SECOND)
        await host.finished()
        observation = observed(host)
    finally:
        await host.stop()
        world.close()

    assert running
    assert observation.cause == "deadline"
    assert observation.result.error_class is ErrorClass.TIMEOUT


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
    resolver = world.resolver(source)
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
        observation = observed(host)
        requested = list(clock.deadlines)
    finally:
        await host.stop()
        world.close()

    assert not before_kill
    if observed_at > 120:
        assert (observed_at + 30) * SECOND not in requested
    assert observation.cause == ("deadline" if cancel_at is None else "cancel")
    assert observation.result.error_class is (
        ErrorClass.TIMEOUT if cancel_at is None else ErrorClass.CANCELLED
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


@pytest.mark.asyncio
async def test_journal_backpressure_never_blocks_stuck_writer(
    tmp_path: Path, kora_capture: Capture
) -> None:
    stall_seconds = 60

    observation = await stuck_writer(tmp_path, kora_capture, stall_seconds)

    assert observation.cause == "journal_unavailable"
    assert observation.result.error_class is ErrorClass.INFRASTRUCTURE
    assert not observation.journal_complete


@pytest.mark.asyncio
async def test_runner_outcome_covers_every_stop_writer_over_stall(
    tmp_path: Path, kora_capture: Capture
) -> None:
    stall_seconds = 30

    observation = await stuck_writer(tmp_path, kora_capture, stall_seconds)

    assert observation.cause == "journal_unavailable"
    assert observation.result.error_class is ErrorClass.INFRASTRUCTURE
