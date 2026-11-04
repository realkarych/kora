import asyncio
import json
import socket
import traceback
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import count
from typing import TypeVar, cast

import pytest

from kora.plugins.harness.codex.app_server_rpc import (
    AppServerRpc,
    Notification,
    RpcError,
    RpcFailure,
)
from tests.plugins.harness.codex.probe import report_rpc_failure

T = TypeVar("T")
FRAME_BUDGET = 131_072
TEST_SECONDS = 5
PRIVATE_FRAME = b"private malformed wire canary"
PRIVATE_MESSAGE = "private error message canary"
PRIVATE_DATA = "private error data canary"


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


@pytest.mark.asyncio
async def test_app_server_rpc_routes_replies_and_notifications() -> None:
    async with wire() as connection:
        observed: list[Notification] = []
        deadline = future_deadline()
        request = connection.start(
            connection.rpc.call(
                "initialize", {}, deadline=deadline, observe=observed.append
            )
        )

        sent = await connection.peer_reader.readline()
        await connection.send(b'{"method":"notification","params":')
        await connection.send(
            b'[1,null],"emittedAtMs":123}\n',
            b'{"method":"empty"}\n',
            b'{"jsonrpc":"2.0","id":1,"result":[true,null]}\n',
            b'{"method":"after","params":false}\n',
        )
        result = await request
        after = await connection.rpc.receive(deadline=deadline)
        await connection.rpc.notify("initialized", deadline=deadline)
        notification = await connection.peer_reader.readline()

        assert sent == b'{"id":1,"method":"initialize","params":{}}\n'
        assert result == [True, None]
        assert observed == [
            Notification("notification", [1, None], 123),
            Notification("empty", None, None),
        ]
        assert after == Notification("after", False, None)
        assert notification == b'{"method":"initialized"}\n'


@pytest.mark.asyncio
async def test_app_server_rpc_remote_refusal_is_safe_and_connection_reusable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        remote_failure = (
            json.dumps(
                {
                    "id": 1,
                    "error": {
                        "code": -32602,
                        "message": PRIVATE_MESSAGE,
                        "data": PRIVATE_DATA,
                    },
                }
            ).encode()
            + b"\n"
        )
        request = connection.start(
            connection.rpc.call("first", {}, deadline=deadline, observe=observed.append)
        )

        assert (
            await connection.peer_reader.readline()
            == b'{"id":1,"method":"first","params":{}}\n'
        )
        await connection.send(remote_failure)
        with pytest.raises(RpcError) as caught:
            await request
        report_status = report_rpc_failure(caught.value)
        second = connection.start(
            connection.rpc.call(
                "second", {}, deadline=deadline, observe=observed.append
            )
        )
        sent = await connection.peer_reader.readline()
        await connection.send(b'{"id":2,"result":null}\n')
        result = await second
        captured = capsys.readouterr()

        assert caught.value.reason is RpcFailure.REMOTE_ERROR
        assert caught.value.args == ("rpc_remote_error",)
        assert caught.value.__context__ is None
        assert captured.out == ""
        assert json.loads(captured.err) == {
            "level": "ERROR",
            "event": "probe.case_failed",
            "detail": "rpc_remote_error",
        }
        assert PRIVATE_MESSAGE not in captured.err
        assert PRIVATE_DATA not in captured.err
        assert report_status == 1
        assert sent == b'{"id":2,"method":"second","params":{}}\n'
        assert result is None
        assert observed == []


@pytest.mark.parametrize(
    ("frame", "failure"),
    [
        (b'{"id":1,"result":"' + PRIVATE_FRAME + b'\xff"}\n', RpcFailure.INVALID_FRAME),
        (b'{"method":"' + PRIVATE_FRAME + b'" broken}\n', RpcFailure.INVALID_FRAME),
        (b'{"id":1,"result":1e309}\n', RpcFailure.INVALID_FRAME),
        (b'{"id":1,"result":NaN}\n', RpcFailure.INVALID_FRAME),
        (b'{"id":1,"result":' + b"9" * 5000 + b"}\n", RpcFailure.INVALID_FRAME),
        (
            b'{"id":1,"result":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n",
            RpcFailure.INVALID_FRAME,
        ),
        (b'{"id":true,"result":{}}\n', RpcFailure.INVALID_FRAME),
        (b'{"id":1.0,"result":{}}\n', RpcFailure.INVALID_FRAME),
        (b'{"id":1,"id":1,"result":{}}\n', RpcFailure.INVALID_FRAME),
        (
            b'{"id":1,"result":{},"error":{"code":0,"message":"x"}}\n',
            RpcFailure.INVALID_FRAME,
        ),
        (b'{"id":1,"error":{"code":true,"message":"x"}}\n', RpcFailure.INVALID_FRAME),
        (b'{"jsonrpc":"1.0","id":1,"result":null}\n', RpcFailure.INVALID_FRAME),
        (b'{"method":"event","emittedAtMs":true}\n', RpcFailure.INVALID_FRAME),
        (b'{"id":2,"result":{}}\n', RpcFailure.UNEXPECTED_REPLY),
        (
            b'{"id":"server-request","method":"item/commandExecution/requestApproval"}\n',
            RpcFailure.SERVER_REQUEST,
        ),
    ],
    ids=[
        "invalid-utf8",
        "invalid-json",
        "float-overflow",
        "nan",
        "long-integer",
        "deep-json",
        "bool-id",
        "float-id",
        "duplicate-id-member",
        "ambiguous-reply",
        "invalid-error-code",
        "wrong-version",
        "invalid-timestamp",
        "wrong-id",
        "server-request",
    ],
)
@pytest.mark.asyncio
async def test_app_server_rpc_refuses_invalid_frames(
    frame: bytes, failure: RpcFailure, capsys: pytest.CaptureFixture[str]
) -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        request = connection.start(
            connection.rpc.call("test", {}, deadline=deadline, observe=observed.append)
        )
        await connection.peer_reader.readline()

        await connection.send(frame)
        with pytest.raises(RpcError) as caught:
            await request
        report_status = report_rpc_failure(caught.value)
        captured = capsys.readouterr()
        rendered = "".join(traceback.format_exception(caught.value))
        with pytest.raises(RpcError) as next_error:
            await connection.rpc.receive(deadline=deadline)

        assert caught.value.reason is failure
        assert caught.value.__context__ is None
        assert caught.value.args == (failure.value,)
        assert PRIVATE_FRAME.decode() not in rendered
        assert PRIVATE_FRAME.decode() not in captured.err
        assert captured.out == ""
        assert report_status == 1
        assert next_error.value.reason is RpcFailure.UNUSABLE
        assert observed == []


@pytest.mark.parametrize("depth", [64, 65])
@pytest.mark.asyncio
async def test_app_server_rpc_json_depth_boundary(depth: int) -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        text = "{[" * 128 + '\\"' + "]}" * 128
        result: object = text
        for _ in range(depth - 1):
            result = [result]
        frame = json.dumps({"id": 1, "result": result}).encode() + b"\n"
        request = connection.start(
            connection.rpc.call("test", {}, deadline=deadline, observe=observed.append)
        )
        await connection.peer_reader.readline()

        await connection.send(frame)
        if depth == 64:
            received = await request
            assert received == result
        else:
            with pytest.raises(RpcError) as caught:
                await request
            assert caught.value.reason is RpcFailure.INVALID_FRAME

        assert observed == []


@pytest.mark.asyncio
async def test_app_server_rpc_duplicate_reply_cannot_settle_later_call() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        first = connection.start(
            connection.rpc.call("first", {}, deadline=deadline, observe=observed.append)
        )
        await connection.peer_reader.readline()

        await connection.send(b'{"id":1,"result":true}\n', b'{"id":1,"result":false}\n')
        result = await first
        with pytest.raises(RpcError) as caught:
            await connection.rpc.call(
                "second", {}, deadline=deadline, observe=observed.append
            )

        assert result is True
        assert caught.value.reason is RpcFailure.UNEXPECTED_REPLY
        assert observed == []


@pytest.mark.asyncio
async def test_app_server_rpc_unsolicited_reply_is_refused() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        await connection.send(b'{"id":1,"result":true}\n')

        with pytest.raises(RpcError) as caught:
            await connection.rpc.receive(deadline=deadline)

        assert caught.value.reason is RpcFailure.UNEXPECTED_REPLY


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.asyncio
async def test_app_server_rpc_frame_limit_before_peer_closes(oversized: bool) -> None:
    async with wire() as connection:
        deadline = future_deadline()
        prefix = b'{"method":"event","params":"'
        suffix = b'"}'
        payload = b"x" * (FRAME_BUDGET - len(prefix) - len(suffix))
        frame = prefix + payload + suffix

        if oversized:
            await connection.send(frame, b"x")
            with pytest.raises(RpcError) as caught:
                await connection.rpc.receive(deadline=deadline)
            assert caught.value.reason is RpcFailure.FRAME_LIMIT
        else:
            await connection.send(frame, b"\n")
            event = await connection.rpc.receive(deadline=deadline)
            assert event == Notification("event", payload.decode(), None)

        assert not connection.peer_writer.is_closing()


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.asyncio
async def test_app_server_rpc_bounds_outbound_encoding(oversized: bool) -> None:
    async with wire(frame_budget=32) as connection:
        deadline = future_deadline()
        method = "界" * (7 if oversized else 6) + "x"

        if oversized:
            with pytest.raises(RpcError) as caught:
                await connection.rpc.notify(method, deadline=deadline)
            connection.writer.close()
            assert caught.value.reason is RpcFailure.FRAME_LIMIT
            assert await connection.peer_reader.read() == b""
        else:
            await connection.rpc.notify(method, deadline=deadline)
            sent = await connection.peer_reader.readline()
            assert sent == ('{"method":"' + method + '"}\n').encode()
            assert len(sent) == 33


@pytest.mark.asyncio
async def test_app_server_rpc_message_limit_counts_mixed_frames() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        first = connection.start(
            connection.rpc.call("first", {}, deadline=deadline, observe=observed.append)
        )
        await connection.peer_reader.readline()
        await connection.send(b'{"method":"seen"}\n' * 254, b'{"id":1,"result":true}\n')

        first_result = await first
        await connection.send(b'{"method":"at-limit"}\n', b'{"id":2,"result":true}\n')
        with pytest.raises(RpcError) as caught:
            await connection.rpc.call(
                "second", {}, deadline=deadline, observe=observed.append
            )
        with pytest.raises(RpcError) as next_error:
            await connection.rpc.receive(deadline=deadline)

        assert first_result is True
        assert observed == [Notification("seen", None, None)] * 254 + [
            Notification("at-limit", None, None)
        ]
        assert caught.value.reason is RpcFailure.MESSAGE_LIMIT
        assert next_error.value.reason is RpcFailure.UNUSABLE


@pytest.mark.asyncio
async def test_app_server_rpc_overlap_preserves_pending_request() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        request = connection.start(
            connection.rpc.call("first", {}, deadline=deadline, observe=observed.append)
        )
        sent = await connection.peer_reader.readline()

        with pytest.raises(RpcError) as caught:
            await connection.rpc.notify("must-not-write", deadline=deadline)
        await connection.send(b'{"id":1,"result":true}\n')
        result = await request
        connection.writer.close()
        rest = await connection.peer_reader.read()

        assert caught.value.reason is RpcFailure.BUSY
        assert sent == b'{"id":1,"method":"first","params":{}}\n'
        assert result is True
        assert rest == b""


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.asyncio
async def test_app_server_rpc_failure_releases_operation(partial: bool) -> None:
    async with wire() as connection:
        deadline = future_deadline()
        if partial:
            await connection.send(b'{"method":"missing-newline"}')
        connection.peer_writer.close()

        with pytest.raises(RpcError) as caught:
            await connection.rpc.receive(deadline=deadline)
        with pytest.raises(RpcError) as next_error:
            await connection.rpc.receive(deadline=deadline)

        assert caught.value.reason is (
            RpcFailure.TRUNCATED if partial else RpcFailure.DISCONNECTED
        )
        assert next_error.value.reason is RpcFailure.UNUSABLE


@pytest.mark.asyncio
async def test_app_server_rpc_cancellation_releases_operation() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        request = connection.start(
            connection.rpc.call("test", {}, deadline=deadline, observe=observed.append)
        )
        sent = await connection.peer_reader.readline()

        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await connection.send(b'{"id":1,"result":true}\n')
        with pytest.raises(RpcError) as caught:
            await connection.rpc.receive(deadline=deadline)

        assert sent == b'{"id":1,"method":"test","params":{}}\n'
        assert request.cancelled()
        assert caught.value.reason is RpcFailure.UNUSABLE
        assert observed == []


@pytest.mark.asyncio
async def test_app_server_rpc_observer_failure_invalidates_pending_reply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[Notification] = []

    def refuse(notification: Notification) -> bool:
        seen.append(notification)
        return False

    async with wire() as connection:
        deadline = future_deadline()
        request = connection.start(
            connection.rpc.call("test", {}, deadline=deadline, observe=refuse)
        )
        await connection.peer_reader.readline()

        payload = json.dumps({"method": "event", "params": PRIVATE_DATA}).encode()
        await connection.send(payload + b"\n", b'{"id":1,"result":true}\n')
        with pytest.raises(RpcError) as refused:
            await request
        report_status = report_rpc_failure(refused.value)
        captured = capsys.readouterr()
        with pytest.raises(RpcError) as caught:
            await connection.rpc.receive(deadline=deadline)

        assert refused.value.reason is RpcFailure.OBSERVER_REFUSED
        assert refused.value.args == ("rpc_observer_refused",)
        assert refused.value.__context__ is None
        assert PRIVATE_DATA not in captured.err
        assert captured.out == ""
        assert report_status == 1
        assert caught.value.reason is RpcFailure.UNUSABLE
        assert seen == [Notification("event", PRIVATE_DATA, None)]


@pytest.mark.asyncio
async def test_app_server_rpc_deadline_applies_to_buffered_notifications() -> None:
    deadline = future_deadline()
    now = deadline - TEST_SECONDS
    observed: list[Notification] = []

    def clock() -> float:
        return now

    def expire(notification: Notification) -> None:
        nonlocal now
        observed.append(notification)
        now = deadline

    async with wire(clock=clock) as connection:
        request = connection.start(
            connection.rpc.call("test", {}, deadline=deadline, observe=expire)
        )
        await connection.peer_reader.readline()

        await connection.send(
            b'{"method":"first"}\n',
            b'{"method":"second"}\n',
            b'{"id":1,"result":true}\n',
        )
        with pytest.raises(RpcError) as caught:
            await request

        assert caught.value.reason is RpcFailure.DEADLINE
        assert observed == [Notification("first", None, None)]


@pytest.mark.asyncio
async def test_app_server_rpc_expired_deadline_writes_zero_bytes() -> None:
    async with wire() as connection:
        deadline = asyncio.get_running_loop().time()

        with pytest.raises(RpcError) as caught:
            await connection.rpc.notify("test", deadline=deadline)
        connection.writer.close()
        sent = await connection.peer_reader.read()

        assert caught.value.reason is RpcFailure.DEADLINE
        assert sent == b""


@pytest.mark.asyncio
async def test_app_server_rpc_stalled_read_reaches_deadline() -> None:
    async with wire() as connection:
        deadline = asyncio.get_running_loop().time() + 0.25
        observed: list[Notification] = []
        request = connection.start(
            connection.rpc.call("test", {}, deadline=deadline, observe=observed.append)
        )
        sent = await connection.peer_reader.readline()

        with pytest.raises(RpcError) as caught:
            await request
        with pytest.raises(RpcError) as next_error:
            await connection.rpc.receive(deadline=future_deadline())

        assert sent == b'{"id":1,"method":"test","params":{}}\n'
        assert caught.value.reason is RpcFailure.DEADLINE
        assert next_error.value.reason is RpcFailure.UNUSABLE
        assert observed == []


@pytest.mark.asyncio
async def test_app_server_rpc_stalled_write_reaches_deadline() -> None:
    async with wire() as connection:
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        peer_transport = connection.peer_writer.transport
        assert isinstance(peer_transport, asyncio.ReadTransport)
        peer_transport.pause_reading()
        connection.writer.transport.set_write_buffer_limits(high=4096, low=1024)
        request = connection.start(
            connection.rpc.notify("x" * 120_000, deadline=loop.time() + 0.25)
        )
        loop.call_soon(writing.set)

        await writing.wait()
        queued = connection.writer.transport.get_write_buffer_size()
        with pytest.raises(RpcError) as caught:
            await request

        assert queued > 4096
        assert caught.value.reason is RpcFailure.DEADLINE


@pytest.mark.asyncio
async def test_app_server_rpc_broken_write_is_disconnected() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        connection.peer_writer.close()
        await connection.peer_writer.wait_closed()
        eof = await connection.reader.read()

        with pytest.raises(RpcError) as caught:
            await connection.rpc.notify("test", deadline=deadline)
        with pytest.raises(RpcError) as next_error:
            await connection.rpc.receive(deadline=deadline)

        assert eof == b""
        assert caught.value.reason is RpcFailure.DISCONNECTED
        assert next_error.value.reason is RpcFailure.UNUSABLE


@pytest.mark.asyncio
async def test_app_server_rpc_rejects_future_reply_already_read() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        first = connection.start(
            connection.rpc.call("first", {}, deadline=deadline, observe=observed.append)
        )
        await connection.peer_reader.readline()

        await connection.send(
            b'{"id":1,"result":true}\n{"method":"between"}\n{"id":2,"result":false}\n'
        )
        result = await first
        with pytest.raises(RpcError) as caught:
            await connection.rpc.call(
                "second", {}, deadline=deadline, observe=observed.append
            )
        connection.writer.close()
        sent = await connection.peer_reader.read()

        assert result is True
        assert caught.value.reason is RpcFailure.UNEXPECTED_REPLY
        assert observed == [Notification("between", None, None)]
        assert sent == b""


@pytest.mark.asyncio
async def test_app_server_rpc_does_not_send_after_observation_budget() -> None:
    async with wire(message_budget=1) as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        await connection.send(b'{"method":"last"}\n')
        last = await connection.rpc.receive(deadline=deadline)

        with pytest.raises(RpcError) as caught:
            await connection.rpc.call(
                "test", {}, deadline=deadline, observe=observed.append
            )
        connection.writer.close()
        sent = await connection.peer_reader.read()

        assert last == Notification("last", None, None)
        assert caught.value.reason is RpcFailure.MESSAGE_LIMIT
        assert observed == []
        assert sent == b""


@pytest.mark.asyncio
async def test_app_server_rpc_rejects_reused_request_identifier() -> None:
    async with wire(request_id=lambda: 7) as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        first = connection.start(
            connection.rpc.call("first", {}, deadline=deadline, observe=observed.append)
        )
        sent = await connection.peer_reader.readline()
        await connection.send(b'{"id":7,"result":true}\n')

        result = await first
        with pytest.raises(RpcError) as caught:
            await connection.rpc.call(
                "second", {}, deadline=deadline, observe=observed.append
            )
        connection.writer.close()
        rest = await connection.peer_reader.read()

        assert sent == b'{"id":7,"method":"first","params":{}}\n'
        assert result is True
        assert caught.value.reason is RpcFailure.REQUEST_ID
        assert rest == b""


@pytest.mark.asyncio
async def test_app_server_rpc_refuses_outbound_container_subclass() -> None:
    class ContainerSubclass(dict[str, object]):
        pass

    async with wire() as connection:
        deadline = future_deadline()
        observed: list[Notification] = []
        params = ContainerSubclass(item="value")

        with pytest.raises(RpcError) as caught:
            await connection.rpc.call(
                "test", params, deadline=deadline, observe=observed.append
            )
        connection.writer.close()
        sent = await connection.peer_reader.read()

        assert caught.value.reason is RpcFailure.INVALID_OUTBOUND
        assert sent == b""
