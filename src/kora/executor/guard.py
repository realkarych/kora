import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from kora.executor.failures import Phase, deadline_due
from kora.protocols.deadlines import (
    NANOSECONDS_PER_SECOND,
    ExecutionDeadline,
    LocalThresholds,
    interruption_due,
    thresholds,
    tighten,
    useful_phase_bound_ns,
)
from kora.protocols.time import Clock

ResultT = TypeVar("ResultT")


class PhaseGuard:
    def __init__(self, clock: Clock, installed: LocalThresholds | None) -> None:
        self.clock = clock
        self.installed = installed

    @classmethod
    def for_deadline(
        cls,
        clock: Clock,
        deadline: ExecutionDeadline | None,
        installed: LocalThresholds | None = None,
    ) -> "PhaseGuard":
        if deadline is None:
            return cls(clock, installed)
        return cls(clock, tighten(installed, thresholds(deadline, clock.sample())))

    def due(self) -> bool:
        return interruption_due(self.installed, self.clock.monotonic_ns())

    def check(self, phase: Phase) -> None:
        if self.due():
            deadline_due(phase)

    def bound_ns(self, own_bound_ns: int, phase: Phase) -> int:
        self.check(phase)
        bound = useful_phase_bound_ns(
            own_bound_ns, self.installed, self.clock.monotonic_ns()
        )
        if bound <= 0:
            deadline_due(phase)
        return bound

    async def within(
        self,
        own_bound_ns: int,
        phase: Phase,
        work: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        remaining_ns = self.bound_ns(own_bound_ns, phase)
        try:
            return await asyncio.wait_for(work(), remaining_ns / NANOSECONDS_PER_SECOND)
        except TimeoutError:
            deadline_due(phase)
