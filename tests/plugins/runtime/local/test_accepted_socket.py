import asyncio
import errno
import inspect
import os
import socket
from collections.abc import Callable, Iterator

import pytest

from kora.conformance.capture import Capture
from kora.plugins.runtime.local.accepted_socket import accept_socket
from kora.protocols.time import SystemClock
from kora.runner.identity import NativeIdentityReader
from kora.runner.peers import kernel_source
from tests.support.local_peers import served, until
from tests.support.local_runners import short_directory
from tests.support.local_runtime import open_descriptors


class AcceptanceClock(SystemClock):
    def __init__(self) -> None:
        self.on_read: Callable[[], None] | None = None

    def monotonic_ns(self) -> int:
        if self.on_read is not None:
            asyncio.get_running_loop().call_soon(self.on_read)
        return super().monotonic_ns()


@pytest.mark.asyncio
async def test_control_connection_cancelled_before_handler_start_closes_socket(
    kora_capture: Capture,
) -> None:
    clock = AcceptanceClock()
    accepted: list[socket.socket] = []
    states: list[str] = []
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.setblocking(False)

    try:
        with short_directory() as base:
            async with served(
                base / "control",
                clock,
                kernel_source(NativeIdentityReader()),
                kora_capture,
            ) as (server, path):

                def cancel_before_start() -> None:
                    if accepted:
                        return
                    for task in asyncio.all_tasks():
                        coroutine = task.get_coro()
                        if not inspect.iscoroutine(coroutine):
                            continue
                        frame = coroutine.cr_frame
                        if (
                            frame is None
                            or frame.f_code.co_name != "_serve"
                            or frame.f_locals.get("self") is not server
                        ):
                            continue
                        endpoint: object = frame.f_locals["endpoint"]
                        assert isinstance(endpoint, socket.socket)
                        accepted.append(endpoint)
                        states.append(inspect.getcoroutinestate(coroutine))
                        task.cancel()
                        clock.on_read = None

                clock.on_read = cancel_before_start
                await asyncio.get_running_loop().sock_connect(peer, path)
                await until(lambda: bool(accepted) and server.connections == 0)

                assert states == [inspect.CORO_CREATED]
                assert accepted[0].fileno() == -1
                assert (
                    await asyncio.wait_for(
                        asyncio.get_running_loop().sock_recv(peer, 1), 5
                    )
                    == b""
                )
    finally:
        peer.close()
        for endpoint in accepted:
            endpoint.close()


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    with short_directory() as base:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
            endpoint.bind(str(base / "accept"))
            endpoint.listen(16)
            endpoint.setblocking(False)
            yield endpoint


@pytest.mark.asyncio
async def test_accepted_socket_stamp_failure_closes_socket(
    listener: socket.socket,
) -> None:
    descriptors = open_descriptors()
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(peer, listener.getsockname())

        with pytest.raises(OSError, match=rf"\[Errno {errno.EBADF}\]"):
            await accept_socket(listener, lambda: os.fstat(-1).st_ino)

        assert (
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 1), 5)
            == b""
        )
    finally:
        peer.close()
    assert open_descriptors() == descriptors


@pytest.mark.asyncio
async def test_accepted_socket_cancelled_handoff_closes_socket(
    listener: socket.socket,
) -> None:
    descriptors = open_descriptors()
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.setblocking(False)
    entered = asyncio.Event()

    async def handler(endpoint: socket.socket, stamp: int) -> None:
        entered.set()
        await asyncio.get_running_loop().sock_recv(endpoint, 1)

    try:
        await asyncio.get_running_loop().sock_connect(peer, listener.getsockname())
        owned = await accept_socket(listener, SystemClock().monotonic_ns)
        task = owned.start(handler)
        with pytest.raises(RuntimeError, match="already handed off"):
            owned.start(handler)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)

        assert isinstance(result[0], asyncio.CancelledError)
        assert not entered.is_set()
        assert owned.endpoint.fileno() == -1
        assert (
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 1), 5)
            == b""
        )
    finally:
        peer.close()
    assert open_descriptors() == descriptors


@pytest.mark.asyncio
async def test_cancelled_accept_leaves_no_unowned_endpoint(
    listener: socket.socket,
) -> None:
    descriptors = open_descriptors()
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.setblocking(False)
    accepting = asyncio.create_task(accept_socket(listener, SystemClock().monotonic_ns))
    barrier = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(barrier.set_result, None)
    try:
        await barrier
        accepting.cancel()
        result = await asyncio.gather(accepting, return_exceptions=True)
        await asyncio.get_running_loop().sock_connect(peer, listener.getsockname())
        owned = await asyncio.wait_for(
            accept_socket(listener, SystemClock().monotonic_ns), 5
        )
        owned.close()

        assert isinstance(result[0], asyncio.CancelledError)
        assert (
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 1), 5)
            == b""
        )
    finally:
        accepting.cancel()
        await asyncio.gather(accepting, return_exceptions=True)
        peer.close()
    assert open_descriptors() == descriptors
