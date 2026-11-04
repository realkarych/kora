import asyncio
import json
import traceback
from pathlib import Path
from typing import Literal

import pytest

from kora.plugins.harness.codex.app_server_rpc import Notification, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_startup import StartupIdentity
from kora.plugins.harness.codex.app_server_turn import (
    TurnError,
    TurnFailure,
    observe_app_server_turn,
)
from tests.plugins.harness.codex.probe import Stdio, report_connection_failure
from tests.plugins.harness.codex.wire_fixture import TEST_SECONDS, future_deadline, wire

THREAD = "selected-thread"
TURN = "started-turn"
PROMPT = "A fixed local prompt\nwith exact text."
PRIVATE = "private wire canary"
STARTED: dict[str, object] = {"turn": {"id": TURN, "status": "inProgress", "items": []}}
NOTICE = b'{"method":"item/started","params":1}\n'
NEXT_NOTICE = b'{"method":"item/completed","params":2}\n'


def frame(data: dict[str, object]) -> bytes:
    return json.dumps(data).encode("utf-8") + b"\n"


def started(result: object = STARTED) -> bytes:
    return frame({"id": 1, "result": result})


def completed(
    *,
    thread: object = THREAD,
    turn: object = TURN,
    status: object = "completed",
    error: object = None,
) -> bytes:
    return frame(
        {
            "method": "turn/completed",
            "params": {
                "threadId": thread,
                "turn": {"id": turn, "status": status, "error": error, "items": []},
            },
        }
    )


@pytest.mark.parametrize(
    ("status", "error", "early"),
    [
        ("completed", None, False),
        ("failed", {"message": PRIVATE, "additionalDetails": PRIVATE}, True),
        ("interrupted", None, False),
    ],
)
@pytest.mark.asyncio
async def test_turn_completes_only_after_matching_start_reply(
    status: str, error: object, early: bool
) -> None:
    async with wire() as connection:
        seen: list[Notification] = []
        waiting = asyncio.Event()
        terminal = completed(status=status, error=error)
        observation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=seen.append,
                on_waiting=waiting.set,
            )
        )

        request = json.loads(await connection.peer_reader.readline())
        if early:
            await connection.send(NOTICE, terminal, NEXT_NOTICE, started())
        else:
            await connection.send(NOTICE, started())
            await waiting.wait()
            await connection.send(NEXT_NOTICE, terminal)
        result = await observation

        assert request == {
            "id": 1,
            "method": "turn/start",
            "params": {
                "threadId": THREAD,
                "input": [{"type": "text", "text": PROMPT}],
            },
        }
        assert result.turn_id == TURN
        assert result.status == status
        assert result.error == error
        assert PRIVATE not in repr(result)
        assert TURN not in repr(result)
        assert seen == [
            Notification("item/started", 1, None),
            Notification("item/completed", 2, None),
        ]
        assert waiting.is_set() is not early


@pytest.mark.parametrize(
    ("frames", "failure"),
    [
        ((started([]),), TurnFailure.INVALID_START),
        (
            (started({"turn": {"id": "", "status": "inProgress"}}),),
            TurnFailure.INVALID_START,
        ),
        (
            (completed(), started({"turn": {"id": TURN, "status": "completed"}})),
            TurnFailure.INVALID_START,
        ),
        (
            (started({"turn": {"id": "\ud800", "status": "inProgress"}}),),
            TurnFailure.INVALID_START,
        ),
        (
            (completed(turn=PRIVATE), started()),
            TurnFailure.IDENTITY_MISMATCH,
        ),
        (
            (started(), completed(thread=PRIVATE)),
            TurnFailure.IDENTITY_MISMATCH,
        ),
        (
            (started(), completed(turn=PRIVATE)),
            TurnFailure.IDENTITY_MISMATCH,
        ),
        (
            (started(), completed(status="inProgress")),
            TurnFailure.INVALID_COMPLETION,
        ),
        (
            (started(), completed(turn=1)),
            TurnFailure.INVALID_COMPLETION,
        ),
        (
            (started(), completed(thread="")),
            TurnFailure.INVALID_COMPLETION,
        ),
        (
            (
                started(),
                completed(status="failed", error={"additionalDetails": PRIVATE}),
            ),
            TurnFailure.INVALID_COMPLETION,
        ),
        (
            (started(), completed(error={"message": PRIVATE})),
            TurnFailure.INVALID_COMPLETION,
        ),
        (
            (completed(), completed(), started()),
            TurnFailure.DUPLICATE_COMPLETION,
        ),
    ],
    ids=[
        "invalid-start-shape",
        "empty-start-id",
        "early-completion-with-invalid-start",
        "invalid-start-id-unicode",
        "early-completion-with-unmatched-start",
        "other-thread",
        "other-turn",
        "nonterminal-status",
        "nontext-turn",
        "empty-thread",
        "invalid-error",
        "successful-turn-with-error",
        "duplicate-early-completion",
    ],
)
@pytest.mark.asyncio
async def test_turn_refuses_invalid_observation_with_fixed_diagnostics(
    frames: tuple[bytes, ...],
    failure: TurnFailure,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with wire() as connection:
        observation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=future_deadline(),
            )
        )
        await connection.peer_reader.readline()

        await connection.send(*frames)
        with pytest.raises(TurnError) as caught:
            await observation
        report_status = report_connection_failure(caught.value)
        captured = capsys.readouterr()
        trace = "".join(traceback.format_exception(caught.value))

        assert caught.value.reason is failure
        assert captured.out == ""
        assert json.loads(captured.err) == {
            "level": "ERROR",
            "event": "probe.case_failed",
            "detail": failure.value,
        }
        assert report_status == 1
        assert PRIVATE not in trace + captured.err


@pytest.mark.asyncio
async def test_already_read_premature_completion_sends_no_turn_request() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        prior = connection.start(
            connection.rpc.call(
                "thread/start", {}, deadline=deadline, observe=lambda _: None
            )
        )
        await connection.peer_reader.readline()
        terminal = completed()
        await connection.send(frame({"id": 1, "result": None}) + terminal[:12])
        assert await prior is None
        await connection.send(terminal[12:])

        with pytest.raises(TurnError) as caught:
            await observe_app_server_turn(
                connection.rpc, thread_id=THREAD, prompt=PROMPT, deadline=deadline
            )
        connection.writer.write_eof()
        remaining = await connection.peer_reader.read()

        assert caught.value.reason is TurnFailure.PREMATURE_COMPLETION
        assert remaining == b""


@pytest.mark.asyncio
async def test_fixture_refuses_a_completion_during_pre_turn_setup() -> None:
    async with wire() as connection:
        identity = StartupIdentity(
            "0.166.0", "linux", Path("/codex"), "kora-probe", "0.1.0"
        )
        stdio = Stdio(connection.rpc, future_deadline(), identity)
        setup = connection.start(stdio.call("thread/start", {}))
        await connection.peer_reader.readline()

        await connection.send(completed(), started())
        with pytest.raises(TurnError) as caught:
            await setup

        assert caught.value.reason is TurnFailure.PREMATURE_COMPLETION


@pytest.mark.parametrize("phase", ["buffered", "before-reply", "after-reply"])
@pytest.mark.asyncio
async def test_turn_preserves_observer_refusal(
    phase: Literal["buffered", "before-reply", "after-reply"],
) -> None:
    async with wire() as connection:
        deadline = future_deadline()
        seen: list[Notification] = []

        def refuse(notice: Notification) -> bool:
            seen.append(notice)
            return False

        waiting = asyncio.Event()
        if phase == "buffered":
            prior = connection.start(
                connection.rpc.call(
                    "thread/start", {}, deadline=deadline, observe=lambda _: None
                )
            )
            await connection.peer_reader.readline()
            await connection.send(frame({"id": 1, "result": None}) + NOTICE)
            assert await prior is None

        observation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=deadline,
                observe=refuse,
                on_waiting=waiting.set,
            )
        )
        if phase != "buffered":
            await connection.peer_reader.readline()
            if phase == "after-reply":
                await connection.send(started())
                await waiting.wait()
            await connection.send(NOTICE)
        with pytest.raises(RpcError) as caught:
            await observation
        with pytest.raises(RpcError) as unusable:
            await connection.rpc.receive(deadline=deadline)

        assert caught.value.reason is RpcFailure.OBSERVER_REFUSED
        assert unusable.value.reason is RpcFailure.UNUSABLE
        assert seen == [Notification("item/started", 1, None)]


@pytest.mark.parametrize("stop", ["cancel", "disconnect", "deadline", "message-budget"])
@pytest.mark.asyncio
async def test_turn_observation_reuses_transport_lifetime_and_budgets(
    stop: Literal["cancel", "disconnect", "deadline", "message-budget"],
) -> None:
    deadline = future_deadline()
    now = deadline - TEST_SECONDS
    waiting = asyncio.Event()
    reading = asyncio.Event()

    def on_waiting() -> None:
        nonlocal now
        if stop == "deadline":
            now = deadline
        waiting.set()

    async with wire(clock=lambda: now, message_budget=2) as connection:
        observation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt=PROMPT,
                deadline=deadline,
                on_waiting=on_waiting,
                observe=lambda _: reading.set(),
            )
        )
        await connection.peer_reader.readline()

        await connection.send(started())
        await waiting.wait()
        if stop == "cancel":
            observation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observation
        else:
            if stop == "disconnect":
                connection.peer_writer.write_eof()
            elif stop == "message-budget":
                await connection.send(NOTICE, completed())
            with pytest.raises(RpcError) as caught:
                await observation
            assert (
                caught.value.reason
                is {
                    "disconnect": RpcFailure.DISCONNECTED,
                    "deadline": RpcFailure.DEADLINE,
                    "message-budget": RpcFailure.MESSAGE_LIMIT,
                }[stop]
            )
        with pytest.raises(RpcError) as unusable:
            await connection.rpc.receive(deadline=future_deadline())

        assert unusable.value.reason is RpcFailure.UNUSABLE
        assert reading.is_set() is (stop == "message-budget")
