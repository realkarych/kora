import heapq
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from kora.plugins.runtime.local.command_stop import (
    CommandDomainStop,
    DomainStop,
    StopOutcome,
)
from kora.protocols.deadlines import ClockSample
from kora.protocols.time import Clock

MAX_TIMING_VALUE = 2**63 - 1
MAX_TIMING_GAPS = 8


def bounded_timing(value: int) -> int:
    return min(MAX_TIMING_VALUE, max(0, value))


@dataclass(frozen=True)
class TimingGap:
    elapsed_ns: int
    thread_cpu_ns: int
    sample_number: int


@dataclass(frozen=True)
class StopTiming:
    factory_calls: int
    stop_entered: bool
    stop_returned: bool
    confirmed: bool
    worker_entry_delay_ns: int | None
    constructor_elapsed_ns: int | None
    constructor_thread_cpu_ns: int | None
    stop_elapsed_ns: int | None
    stop_thread_cpu_ns: int | None
    clock_samples: int
    largest_gaps: tuple[TimingGap, ...]


class MeasuredClock:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.samples = 0
        self.previous: tuple[int, int] | None = None
        self.gaps: list[tuple[int, int, int]] = []

    def now(self) -> datetime:
        return self.clock.now()

    def monotonic_ns(self) -> int:
        sampled = self.clock.monotonic_ns()
        cpu = time.thread_time_ns()
        self.record(sampled, cpu)
        return sampled

    def record(self, sampled: int, cpu: int) -> None:
        self.samples = bounded_timing(self.samples + 1)
        previous = self.previous
        self.previous = sampled, cpu
        if previous is None:
            return
        gap = (
            bounded_timing(sampled - previous[0]),
            bounded_timing(cpu - previous[1]),
            self.samples,
        )
        if len(self.gaps) < MAX_TIMING_GAPS:
            heapq.heappush(self.gaps, gap)
        elif gap > self.gaps[0]:
            heapq.heapreplace(self.gaps, gap)

    def sample(self) -> ClockSample:
        return self.clock.sample()

    async def sleep_until(self, deadline_ns: int) -> None:
        await self.clock.sleep_until(deadline_ns)


class MeasuredStop:
    def __init__(
        self, original: DomainStop, probe: "StopTimingProbe", entered_ns: int
    ) -> None:
        self.original = original
        self.probe = probe
        self.entered_ns = entered_ns

    def stop(self, started_ns: int) -> StopOutcome:
        self.probe.stop_entered = True
        self.probe.worker_entry_delay_ns = bounded_timing(self.entered_ns - started_ns)
        wall = time.monotonic_ns()
        cpu = time.thread_time_ns()
        try:
            outcome = self.original.stop(started_ns)
            self.probe.stop_returned = True
            self.probe.confirmed = outcome is StopOutcome.CONFIRMED
            return outcome
        finally:
            self.probe.stop_elapsed_ns = bounded_timing(time.monotonic_ns() - wall)
            self.probe.stop_thread_cpu_ns = bounded_timing(time.thread_time_ns() - cpu)


class StopTimingProbe:
    def __init__(self) -> None:
        self.factory_calls = 0
        self.stop_entered = False
        self.stop_returned = False
        self.confirmed = False
        self.worker_entry_delay_ns: int | None = None
        self.constructor_elapsed_ns: int | None = None
        self.constructor_thread_cpu_ns: int | None = None
        self.stop_elapsed_ns: int | None = None
        self.stop_thread_cpu_ns: int | None = None
        self.measured_clock: MeasuredClock | None = None

    def __call__(
        self,
        group: int,
        marker: bytes,
        clock: Clock,
        wake_reaper: Callable[[], None],
        prelaunch: dict[int, int] | None,
    ) -> DomainStop:
        entered = clock.monotonic_ns()
        wall = time.monotonic_ns()
        cpu = time.thread_time_ns()
        self.factory_calls = bounded_timing(self.factory_calls + 1)
        measured = MeasuredClock(clock)
        self.measured_clock = measured
        try:
            original = CommandDomainStop(
                group, marker, measured, wake_reaper, prelaunch
            )
        finally:
            self.constructor_elapsed_ns = bounded_timing(time.monotonic_ns() - wall)
            self.constructor_thread_cpu_ns = bounded_timing(time.thread_time_ns() - cpu)
        return MeasuredStop(original, self, entered)

    def snapshot(self) -> StopTiming:
        measured = self.measured_clock
        return StopTiming(
            factory_calls=self.factory_calls,
            stop_entered=self.stop_entered,
            stop_returned=self.stop_returned,
            confirmed=self.confirmed,
            worker_entry_delay_ns=self.worker_entry_delay_ns,
            constructor_elapsed_ns=self.constructor_elapsed_ns,
            constructor_thread_cpu_ns=self.constructor_thread_cpu_ns,
            stop_elapsed_ns=self.stop_elapsed_ns,
            stop_thread_cpu_ns=self.stop_thread_cpu_ns,
            clock_samples=0 if measured is None else measured.samples,
            largest_gaps=()
            if measured is None
            else tuple(TimingGap(*gap) for gap in sorted(measured.gaps, reverse=True)),
        )
