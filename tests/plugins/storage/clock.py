from datetime import UTC, datetime

from kora.protocols.deadlines import ClockSample


class StorageClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 10, 24, tzinfo=UTC)
        self.ticks = 0

    def now(self) -> datetime:
        return self.wall

    def monotonic_ns(self) -> int:
        return self.ticks

    def sample(self) -> ClockSample:
        return ClockSample(wall=self.wall, monotonic_ns=self.ticks)

    async def sleep_until(self, deadline_ns: int) -> None:
        self.ticks = max(self.ticks, deadline_ns)
