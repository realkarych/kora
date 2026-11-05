import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from kora.plugins.harness.codex.app_server_rpc import (
    AppServerRpc,
    ControlRequest,
    Notification,
)

TerminalStatus = Literal["completed", "failed", "interrupted"]
TERMINAL_METHOD = "turn/completed"


class TurnFailure(StrEnum):
    INVALID_START = "turn_invalid_start"
    INVALID_COMPLETION = "turn_invalid_completion"
    IDENTITY_MISMATCH = "turn_identity_mismatch"
    PREMATURE_COMPLETION = "turn_premature_completion"
    DUPLICATE_COMPLETION = "turn_duplicate_completion"
    INVALID_INTERRUPT = "turn_invalid_interrupt"


class TurnError(Exception):
    def __init__(self, reason: TurnFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, repr=False)
class TurnCompletion:
    turn_id: str
    status: TerminalStatus
    error: dict[str, object] | None


def object_fields(value: object, failure: TurnFailure) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TurnError(failure)
    return cast("dict[str, object]", value)


def identifier(value: object, failure: TurnFailure) -> str:
    if isinstance(value, str) and value:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            pass
        else:
            return value
    raise TurnError(failure)


def started_turn_id(reply: object) -> str:
    failure = TurnFailure.INVALID_START
    turn = object_fields(object_fields(reply, failure).get("turn"), failure)
    if turn.get("status") != "inProgress":
        raise TurnError(failure)
    return identifier(turn.get("id"), failure)


def terminal_observation(params: object, thread_id: str) -> TurnCompletion:
    failure = TurnFailure.INVALID_COMPLETION
    fields = object_fields(params, failure)
    reported_thread = identifier(fields.get("threadId"), failure)
    turn = object_fields(fields.get("turn"), failure)
    turn_id = identifier(turn.get("id"), failure)
    status = turn.get("status")
    if status not in ("completed", "failed", "interrupted"):
        raise TurnError(failure)
    if reported_thread != thread_id:
        raise TurnError(TurnFailure.IDENTITY_MISMATCH)
    error = turn.get("error")
    if error is not None:
        error = object_fields(error, failure)
        if status == "completed" or not isinstance(error.get("message"), str):
            raise TurnError(failure)
    return TurnCompletion(turn_id, status, error)


def reject_premature_completion(notification: Notification) -> None:
    if notification.method == TERMINAL_METHOD:
        raise TurnError(TurnFailure.PREMATURE_COMPLETION)


async def observe_app_server_turn(
    rpc: AppServerRpc,
    *,
    thread_id: str,
    prompt: str,
    deadline: float,
    observe: Callable[[Notification], bool | None] | None = None,
    observe_before_send: Callable[[Notification], bool | None] | None = None,
    on_waiting: Callable[[], None] | None = None,
    on_started: Callable[[str], None] | None = None,
    stop: asyncio.Event | None = None,
) -> TurnCompletion:
    identifier(thread_id, TurnFailure.INVALID_START)
    completion: TurnCompletion | None = None
    turn_id: str | None = None

    def observe_buffered(notification: Notification) -> bool | None:
        reject_premature_completion(notification)
        callback = observe if observe_before_send is None else observe_before_send
        return callback(notification) if callback is not None else None

    def observe_pending(notification: Notification) -> bool | None:
        nonlocal completion
        if notification.method != TERMINAL_METHOD:
            return observe(notification) if observe is not None else None
        if completion is not None:
            raise TurnError(TurnFailure.DUPLICATE_COMPLETION)
        terminal = terminal_observation(notification.params, thread_id)
        if turn_id is not None and terminal.turn_id != turn_id:
            raise TurnError(TurnFailure.IDENTITY_MISMATCH)
        completion = terminal
        return None

    turn_id = started_turn_id(
        await rpc.call(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
            deadline=deadline,
            observe=observe_pending,
            observe_before_send=observe_buffered,
        )
    )
    if completion is not None and completion.turn_id != turn_id:
        raise TurnError(TurnFailure.IDENTITY_MISMATCH)
    rpc.check_deadline(deadline)
    if on_started is not None:
        on_started(turn_id)
    if completion is None and on_waiting is not None:
        on_waiting()
    if completion is None and stop is not None:

        def accept_interrupt(reply: object) -> None:
            if not isinstance(reply, dict) or reply:
                raise TurnError(TurnFailure.INVALID_INTERRUPT)

        await rpc.receive_until(
            deadline=deadline,
            observe=observe_pending,
            complete=lambda: completion is not None,
            control=ControlRequest(
                stop,
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                accept_interrupt,
            ),
        )
    while completion is None:
        await rpc.receive(deadline=deadline, observe=observe_pending)
    return completion
