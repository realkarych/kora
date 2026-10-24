import asyncio
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager


class ReaderHandoffActivity:
    def __init__(self) -> None:
        self.undelivered = asyncio.Event()
        self.release_handoff = asyncio.Event()
        self.reader_closed = asyncio.Event()

    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        return self.record(name)

    @asynccontextmanager
    async def record(self, name: str) -> AsyncGenerator[None]:
        yield
        if name == "storage.read_stage.admitted":
            self.undelivered.set()
            await self.release_handoff.wait()
        if name == "storage.reader.close":
            self.reader_closed.set()


class StorageBarrierActivity:
    def __init__(self, boundary: str, *, on_exit: bool = False) -> None:
        self.boundary = boundary
        self.on_exit = on_exit
        self.entered = asyncio.Event()
        self.released = asyncio.Event()
        self.close_started = asyncio.Event()

    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        return self.record(name)

    @asynccontextmanager
    async def record(self, name: str) -> AsyncGenerator[None]:
        if name == "storage.reader.close_pending":
            self.close_started.set()
        if name == self.boundary and not self.on_exit:
            self.entered.set()
            await self.released.wait()
        yield
        if name == self.boundary and self.on_exit:
            self.entered.set()
            await self.released.wait()
