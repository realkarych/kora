import asyncio
import json
import traceback
from collections.abc import Callable
from typing import Literal

import pytest

from kora.plugins.harness.codex.app_server_rpc import Notification, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_turn import (
    TerminalStatus,
    TurnCompletion,
    TurnError,
    TurnFailure,
    observe_app_server_turn,
)
from tests.plugins.harness.codex.test_app_server_turn import (
    NOTICE,
    PRIVATE,
    PROMPT,
    THREAD,
    TURN,
    completed,
    frame,
    started,
)
from tests.plugins.harness.codex.wire_fixture import Wire, future_deadline, wire


def owned_tasks() -> set[asyncio.Task[object]]:
    return {
        task
        for task in asyncio.all_tasks()
        if task.get_name() in {"codex-rpc-read", "codex-rpc-stop"}
    }


async def scheduled_barrier() -> None:
    ready = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready.set)
    await ready.wait()


async def acknowledge_start(connection: Wire) -> None:
    request: object = json.loads(await connection.peer_reader.readline())
    assert request == {
        "id": 1,
        "method": "turn/start",
        "params": {
            "threadId": THREAD,
            "input": [{"type": "text", "text": PROMPT}],
        },
    }
    await connection.send(started())


async def interrupt_request(connection: Wire) -> None:
    request: object = json.loads(await connection.peer_reader.readline())
    assert request == {
        "id": 2,
        "method": "turn/interrupt",
        "params": {"threadId": THREAD, "turnId": TURN},
    }


@pytest.mark.parametrize("terminal_first", [False, True])
@pytest.mark.parametrize("status", ["interrupted", "completed", "failed"])
@pytest.mark.asyncio
async def test_stop_requires_reply_and_terminal_and_preserves_native_status(
    terminal_first: bool, status: TerminalStatus
) -> None:
    async with wire() as connection:
        stop = asyncio.Event()
        waiting = asyncio.Event()
        seen: list[Notification] = []
        acknowledged: list[str] = []
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=seen.append,
                on_started=acknowledged.append,
                on_waiting=waiting.set,
                stop=stop,
            )
        )
        await acknowledge_start(connection)
        await waiting.wait()
        stop.set()
        await interrupt_request(connection)
        reply = frame({"id": 2, "result": {}})
        terminal = completed(status=status)
        first, second = (terminal, reply) if terminal_first else (reply, terminal)

        await connection.send(first)
        await scheduled_barrier()
        assert not operation.done()
        await connection.send(second, NOTICE)
        result = await operation
        remaining = await connection.rpc.receive(deadline=future_deadline())

        assert result == TurnCompletion(TURN, status, None)
        assert acknowledged == [TURN]
        assert seen == []
        assert remaining == Notification("item/started", 1, None)
        assert stop.is_set()
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_stop_pending_before_start_waits_for_acknowledged_identity() -> None:
    async with wire() as connection:
        stop = asyncio.Event()
        stop.set()
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                stop=stop,
            )
        )

        await acknowledge_start(connection)
        await interrupt_request(connection)
        await connection.send(frame({"id": 2, "result": {}}), completed())
        result = await operation

        assert result.status == "completed"
        assert stop.is_set()
        assert owned_tasks() == set()


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.asyncio
async def test_already_ready_terminal_wins_over_stop_without_control_bytes(
    early: bool,
) -> None:
    async with wire() as connection:
        stop = asyncio.Event()
        stop.set()
        acknowledged: list[str] = []
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                on_started=acknowledged.append,
                stop=stop,
            )
        )
        await connection.peer_reader.readline()
        messages = (completed(), started()) if early else (started(), completed())

        await connection.send(*messages)
        result = await operation
        connection.writer.write_eof()
        control_bytes = await connection.peer_reader.read()

        assert result.status == "completed"
        assert acknowledged == [TURN]
        assert control_bytes == b""
        assert stop.is_set()
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_interrupt_keeps_the_pending_partial_utf8_frame_and_exclusive_owner() -> (
    None
):
    async with wire() as connection:
        stop = asyncio.Event()
        waiting = asyncio.Event()
        seen: list[Notification] = []
        notice = '{"method":"item/started","params":"é"}\n'.encode()
        boundary = notice.index(b"\xc3") + 1
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                stop=stop,
                observe=seen.append,
                on_waiting=waiting.set,
            )
        )
        await connection.peer_reader.readline()
        await connection.send(started(), notice[:boundary])
        await waiting.wait()
        await scheduled_barrier()
        assert {task.get_name() for task in owned_tasks()} == {
            "codex-rpc-read",
            "codex-rpc-stop",
        }

        with pytest.raises(RpcError) as busy:
            await connection.rpc.notify("competing", deadline=future_deadline())
        stop.set()
        await interrupt_request(connection)
        assert len(owned_tasks()) == 1
        await connection.send(
            notice[boundary:], frame({"id": 2, "result": {}}), completed()
        )
        result = await operation

        assert busy.value.reason is RpcFailure.BUSY
        assert result.status == "completed"
        assert seen == [Notification("item/started", "é", None)]
        assert owned_tasks() == set()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"id": 2, "result": None}, TurnFailure.INVALID_INTERRUPT),
        ({"id": 2, "result": []}, TurnFailure.INVALID_INTERRUPT),
        ({"id": 2, "result": {PRIVATE: PRIVATE}}, TurnFailure.INVALID_INTERRUPT),
        ({"id": 3, "result": {}}, RpcFailure.UNEXPECTED_REPLY),
        (
            {"id": 2, "error": {"code": -1, "message": PRIVATE, "data": PRIVATE}},
            RpcFailure.REMOTE_ERROR,
        ),
        ({"method": PRIVATE, "id": 2}, RpcFailure.SERVER_REQUEST),
    ],
)
@pytest.mark.asyncio
async def test_control_reply_failures_are_immediate_content_free_and_fail_closed(
    message: dict[str, object], expected: TurnFailure | RpcFailure
) -> None:
    async with wire() as connection:
        stop = asyncio.Event()
        stop.set()
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                stop=stop,
            )
        )
        await acknowledge_start(connection)
        await interrupt_request(connection)

        await connection.send(frame(message))
        with pytest.raises((RpcError, TurnError)) as caught:
            await operation
        with pytest.raises(RpcError) as unusable:
            await connection.rpc.receive(deadline=future_deadline())

        assert caught.value.reason is expected
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert unusable.value.reason is RpcFailure.UNUSABLE
        assert stop.is_set()
        assert owned_tasks() == set()


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ((completed(thread=PRIVATE),), TurnFailure.IDENTITY_MISMATCH),
        ((completed(turn=PRIVATE),), TurnFailure.IDENTITY_MISMATCH),
        ((completed(), completed()), TurnFailure.DUPLICATE_COMPLETION),
        (
            (frame({"id": 2, "result": {}}), frame({"id": 2, "result": {}})),
            RpcFailure.UNEXPECTED_REPLY,
        ),
        ((NOTICE,), RpcFailure.OBSERVER_REFUSED),
    ],
)
@pytest.mark.asyncio
async def test_stream_failures_during_control_settle_every_owned_task(
    messages: tuple[bytes, ...], expected: TurnFailure | RpcFailure
) -> None:
    async with wire() as connection:
        stop = asyncio.Event()
        stop.set()
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                stop=stop,
                observe=lambda _: False,
            )
        )
        await acknowledge_start(connection)
        await interrupt_request(connection)

        await connection.send(*messages)
        with pytest.raises((RpcError, TurnError)) as caught:
            await operation

        assert caught.value.reason is expected
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert owned_tasks() == set()


@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("ending", ["cancel", "disconnect", "truncated", "deadline"])
@pytest.mark.asyncio
async def test_controlled_read_lifetime_ends_on_cancellation_failure_and_deadline(
    controlled: bool, ending: Literal["cancel", "disconnect", "truncated", "deadline"]
) -> None:
    async with wire() as connection:
        stop = asyncio.Event()
        waiting = asyncio.Event()
        deadline = (
            asyncio.get_running_loop().time() + 0.25
            if ending == "deadline"
            else future_deadline()
        )
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=deadline,
                stop=stop,
                on_waiting=waiting.set,
            )
        )
        await acknowledge_start(connection)
        await waiting.wait()
        await scheduled_barrier()
        if controlled:
            stop.set()
            await interrupt_request(connection)

        if ending == "cancel":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            if ending == "truncated":
                await connection.send(b'{"method":')
            if ending in {"disconnect", "truncated"}:
                connection.peer_writer.write_eof()
            with pytest.raises(RpcError) as caught:
                await operation
            assert (
                caught.value.reason
                is {
                    "disconnect": RpcFailure.DISCONNECTED,
                    "truncated": RpcFailure.TRUNCATED,
                    "deadline": RpcFailure.DEADLINE,
                }[ending]
            )
        with pytest.raises(RpcError) as unusable:
            await connection.rpc.receive(deadline=future_deadline())

        assert unusable.value.reason is RpcFailure.UNUSABLE
        assert stop.is_set() is controlled
        assert owned_tasks() == set()


@pytest.mark.parametrize(
    ("message_budget", "request_id", "payload", "expected"),
    [
        (1, None, None, RpcFailure.MESSAGE_LIMIT),
        (3, lambda: 1, None, RpcFailure.REQUEST_ID),
        (3, None, b"x" * 301, RpcFailure.FRAME_LIMIT),
        (3, None, frame({"method": "notice"}) * 4, RpcFailure.MESSAGE_LIMIT),
    ],
)
@pytest.mark.asyncio
async def test_controlled_receive_preserves_existing_transport_budgets(
    message_budget: int,
    request_id: Callable[[], int] | None,
    payload: bytes | None,
    expected: RpcFailure,
) -> None:
    async with wire(
        frame_budget=300, message_budget=message_budget, request_id=request_id
    ) as connection:
        stop = asyncio.Event()
        stop.set()
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                stop=stop,
            )
        )
        await acknowledge_start(connection)

        if payload is not None:
            await interrupt_request(connection)
            await connection.send(payload)
        with pytest.raises(RpcError) as caught:
            await operation

        assert caught.value.reason is expected
        assert owned_tasks() == set()


@pytest.mark.asyncio
async def test_unrequested_stop_waiter_is_released_after_natural_completion() -> None:
    async with wire() as connection:
        stop = asyncio.Event()
        waiting = asyncio.Event()
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                stop=stop,
                on_waiting=waiting.set,
            )
        )
        await acknowledge_start(connection)
        await waiting.wait()
        await scheduled_barrier()
        assert len(owned_tasks()) == 2

        await connection.send(completed())
        result = await operation
        stop.set()
        assert await stop.wait()

        assert result.status == "completed"
        assert owned_tasks() == set()
