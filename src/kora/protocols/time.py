import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

from kora.protocols.deadlines import NANOSECONDS_PER_SECOND, ClockSample


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...

    def sample(self) -> ClockSample: ...

    async def sleep_until(self, deadline_ns: int) -> None: ...


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def sample(self) -> ClockSample:
        return ClockSample(wall=self.now(), monotonic_ns=self.monotonic_ns())

    async def sleep_until(self, deadline_ns: int) -> None:
        remaining = deadline_ns - self.monotonic_ns()
        if remaining > 0:
            await asyncio.sleep(remaining / NANOSECONDS_PER_SECOND)
