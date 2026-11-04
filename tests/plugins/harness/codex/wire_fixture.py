import asyncio
import socket
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import count
from typing import TypeVar, cast

from kora.plugins.harness.codex.app_server_rpc import AppServerRpc

T = TypeVar("T")
FRAME_BUDGET = 131_072
TEST_SECONDS = 5


@dataclass
class Wire:
    rpc: AppServerRpc
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer_reader: asyncio.StreamReader
    peer_writer: asyncio.StreamWriter
    tasks: list[asyncio.Future[object]] = field(
        default_factory=list[asyncio.Future[object]]
    )

    def start(self, operation: Coroutine[object, object, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(operation)
        self.tasks.append(cast("asyncio.Future[object]", task))
        return task

    async def send(self, *frames: bytes) -> None:
        self.peer_writer.writelines(frames)
        await self.peer_writer.drain()

    async def close(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.peer_writer.transport.abort()
        self.writer.transport.abort()
        for writer in (self.peer_writer, self.writer):
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                assert writer.is_closing()


@asynccontextmanager
async def wire(
    *,
    frame_budget: int = FRAME_BUDGET,
    message_budget: int = 256,
    clock: Callable[[], float] | None = None,
    request_id: Callable[[], int] | None = None,
) -> AsyncGenerator[Wire]:
    async with asyncio.timeout(TEST_SECONDS):
        local, remote = socket.socketpair()
        with local, remote:
            local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            reader, writer = await asyncio.open_connection(
                sock=local, limit=FRAME_BUDGET * 2
            )
            peer_reader, peer_writer = await asyncio.open_connection(
                sock=remote, limit=FRAME_BUDGET * 2
            )
            rpc = AppServerRpc(
                reader,
                writer,
                clock=clock if clock is not None else asyncio.get_running_loop().time,
                next_request_id=request_id
                if request_id is not None
                else count(1).__next__,
                max_frame_bytes=frame_budget,
                max_messages=message_budget,
            )
            connection = Wire(rpc, reader, writer, peer_reader, peer_writer)
            try:
                yield connection
            finally:
                async with asyncio.timeout(TEST_SECONDS):
                    await connection.close()
                assert local.fileno() == remote.fileno() == -1


def future_deadline() -> float:
    return asyncio.get_running_loop().time() + TEST_SECONDS
