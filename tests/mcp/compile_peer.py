import asyncio
import socket
import sys
from pathlib import Path
from threading import Event
from types import TracebackType
from typing import Self

from anyio import EndOfStream, wait_all_tasks_blocked
from mcp.server.stdio import stdio_server
from mcp.shared._stream_protocols import ReadStream
from mcp.shared.message import SessionMessage

from kora.client.settings import ClientSettings
from kora.conformance.capture import captured
from kora.dsl import CompiledGraph, CompileFindings, compile_graph
from kora.mcp.results import MCP_COMPONENT
from kora.mcp.stdio import create_authoring_server
from kora.observability import LoggingSettings
from tests.support.observability import FakeClock

DEADLINE_SECONDS = 60.0


class GatedCompiler:
    def __init__(self, held: Path) -> None:
        self.held = held
        self.loop = asyncio.get_running_loop()
        self.opened = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = Event()
        self.selected: list[Path] = []

    def __call__(self, path: Path) -> CompiledGraph | CompileFindings:
        self.selected.append(path)
        if path != self.held:
            return compile_graph(path)
        try:
            with path.open("rb") as source:
                assert source.read(1)
                self.loop.call_soon_threadsafe(self.opened.set)
                assert self.release.wait(DEADLINE_SECONDS)
                return compile_graph(path)
        finally:
            self.loop.call_soon_threadsafe(self.closed.set)


class ObservedInput:
    def __init__(self, stream: ReadStream[SessionMessage | Exception]) -> None:
        self.stream = stream
        self.eof = asyncio.Event()

    async def receive(self) -> SessionMessage | Exception:
        try:
            return await self.stream.receive()
        except EndOfStream:
            self.eof.set()
            raise

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        try:
            return await self.receive()
        except EndOfStream:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.stream.aclose()


async def run_peer(control: socket.socket, path: Path) -> None:
    control.setblocking(False)
    loop = asyncio.get_running_loop()
    compiler = GatedCompiler(path)
    settings = ClientSettings("http://127.0.0.1:9", "token")
    with captured(
        MCP_COMPONENT, LoggingSettings(), "compile-peer", FakeClock().now
    ) as capture:
        async with (
            settings.client(capture.diagnostics) as client,
            stdio_server() as (
                requests,
                responses,
            ),
        ):
            incoming = ObservedInput(requests)
            server = create_authoring_server(
                client, capture.diagnostics, compile_source=compiler
            )
            serving = asyncio.create_task(
                server.run(incoming, responses, server.create_initialization_options())
            )
            try:
                async with asyncio.timeout(DEADLINE_SECONDS):
                    await compiler.opened.wait()
                    await loop.sock_sendall(control, b"O")
                    await incoming.eof.wait()
                    await wait_all_tasks_blocked()
                    assert not serving.done()
                    assert not compiler.closed.is_set()
                    await loop.sock_sendall(control, b"E")
                    assert await loop.sock_recv(control, 1) == b"R"
                    compiler.release.set()
                    await serving
                    assert compiler.closed.is_set()
                    await loop.sock_sendall(control, b"C")
            finally:
                compiler.release.set()
                await serving


if __name__ == "__main__":
    with socket.socket(fileno=int(sys.argv[1])) as endpoint:
        asyncio.run(run_peer(endpoint, Path(sys.argv[2])))
