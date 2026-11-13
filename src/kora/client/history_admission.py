import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from anyio import CancelScope


class HistoryAdmission:
    def __init__(self, connections: int) -> None:
        self.connections = connections
        self.condition = asyncio.Condition()
        self.histories: set[CancelScope] = set()
        self.ordinary = 0

    @asynccontextmanager
    async def enter(self, scope: CancelScope, *, history: bool) -> AsyncGenerator[None]:
        admitted = False
        try:
            async with self.condition:
                if history:
                    await self.condition.wait_for(
                        lambda: (
                            len(self.histories) < max(1, self.connections - 1)
                            and (self.connections > 1 or self.ordinary == 0)
                        )
                    )
                    self.histories.add(scope)
                    admitted = True
                else:
                    self.ordinary += 1
                    admitted = True
                    if self.connections == 1:
                        for pending in self.histories:
                            pending.cancel()
                        await self.condition.wait_for(lambda: not self.histories)
            yield
        finally:
            if admitted:
                with CancelScope(shield=True):
                    async with self.condition:
                        if history:
                            self.histories.remove(scope)
                        else:
                            self.ordinary -= 1
                        self.condition.notify_all()
