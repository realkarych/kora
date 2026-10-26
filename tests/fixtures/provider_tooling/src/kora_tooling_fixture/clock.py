import asyncio

from kora.protocols.time import SystemClock


class ProbeClock(SystemClock):
    def __init__(self) -> None:
        self.instant = super().monotonic_ns()
        self.waiters: dict[asyncio.Future[None], int] = {}
        self.changed = asyncio.Event()
        self.real_started: int | None = None

    def monotonic_ns(self) -> int:
        return self.instant + (
            0
            if self.real_started is None
            else super().monotonic_ns() - self.real_started
        )

    async def sleep_until(self, deadline_ns: int) -> None:
        if self.real_started is not None:
            await super().sleep_until(deadline_ns)
            return
        if self.instant >= deadline_ns:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters[waiter] = deadline_ns
        self.changed.set()
        try:
            await waiter
        finally:
            self.waiters.pop(waiter)
            self.changed.set()

    def use_system_time(self) -> None:
        self.real_started = super().monotonic_ns()
        loop = asyncio.get_running_loop()
        for waiter, deadline in tuple(self.waiters.items()):
            handle = loop.call_later(
                max(0, deadline - self.instant) / 1_000_000_000, self.wake, waiter
            )
            waiter.add_done_callback(lambda _, timer=handle: timer.cancel())

    @staticmethod
    def wake(waiter: asyncio.Future[None]) -> None:
        if not waiter.done():
            waiter.set_result(None)

    async def advance_to_timer(
        self, instant: int, *, current: int | None = None
    ) -> None:
        while instant not in self.waiters.values():
            self.changed.clear()
            if instant not in self.waiters.values():
                await self.changed.wait()
        self.instant = instant if current is None else current
        for waiter, deadline in tuple(self.waiters.items()):
            if deadline <= self.instant and not waiter.done():
                waiter.set_result(None)
