import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Protocol

from kora.ir.json import parse_json_object
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.codex.app_server_rpc import AppServerRpc
from kora.plugins.harness.codex.app_server_session import AppServerTurnSession
from kora.plugins.harness.codex.app_server_startup import StartupIdentity
from kora.plugins.harness.codex.app_server_turn import TurnError, TurnFailure
from kora.plugins.harness.codex.app_server_usage import UsageObserver
from kora.plugins.harness.codex.selected_startup import (
    SelectedEnvironment,
    prepare_selected_thread,
)
from kora.plugins.harness.codex.terminal_result import (
    TerminalResult,
    project_terminal_result,
)
from kora.plugins.harness.codex.turn_events import CodexTurnEvents
from kora.plugins.harness.codex.usage_events import CodexUsageEvents, UsageStamp
from kora.protocols.harness import EventSink, NativeLaunch


class BoundAppStream(Protocol):
    @property
    def stream(self) -> NativeLaunch: ...

    @property
    def stream_limit(self) -> int: ...

    def admit_root_turn(self) -> None: ...


@dataclass(frozen=True, repr=False, slots=True)
class DirectFixtureRpc:
    rpc: AppServerRpc


class BoundTurnRpc(AppServerRpc):
    def __init__(
        self,
        app: BoundAppStream,
        *,
        clock: Callable[[], float],
        next_request_id: Callable[[], int],
        max_messages: int,
    ) -> None:
        stream = app.stream
        super().__init__(
            stream.reader,
            stream.writer,
            clock=clock,
            next_request_id=next_request_id,
            max_frame_bytes=app.stream_limit,
            max_messages=max_messages,
            max_read_bytes=app.stream_limit,
        )
        self._app = app

    @property
    def rpc(self) -> AppServerRpc:
        return self

    async def _send(
        self,
        message: dict[str, object],
        deadline: float,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        if message.get("method") != "turn/start":
            await super()._send(message, deadline, before_write)
            return

        def admit_root_turn() -> None:
            if before_write is not None:
                before_write()
            self._app.admit_root_turn()

        await super()._send(message, deadline, admit_root_turn)


@dataclass(frozen=True, repr=False)
class SelectedTurnInputs:
    prompt: str
    output_schema: dict[str, object] | None
    output_required: bool
    max_messages: int
    max_tools: int


async def run_selected_turn(
    control: BoundTurnRpc | DirectFixtureRpc,
    identity: StartupIdentity,
    expected: SelectedEnvironment,
    bearer: str,
    inputs: SelectedTurnInputs,
    *,
    deadline: float,
    sink: EventSink,
    registry: SecretRegistry,
    usage_message_id: str,
    source_time: Callable[[], datetime],
    next_usage_stamp: Callable[[], UsageStamp],
    stop: asyncio.Event | None = None,
) -> TerminalResult:
    rpc = control.rpc

    def require_running() -> None:
        if stop is not None and stop.is_set():
            raise TurnError(TurnFailure.STOP_BEFORE_START)

    check_deadline = partial(rpc.check_deadline, deadline)
    messages = CodexTurnEvents(sink, registry=registry, check_deadline=check_deadline)
    usage = CodexUsageEvents(
        sink,
        message_id=usage_message_id,
        next_stamp=next_usage_stamp,
        check_deadline=check_deadline,
    )
    thread_id = await prepare_selected_thread(
        rpc, identity, expected, bearer, deadline=deadline, before_write=require_running
    )
    session = AppServerTurnSession(
        thread_id, max_messages=inputs.max_messages, max_tools=inputs.max_tools
    )
    await session.run(
        rpc,
        prompt=inputs.prompt,
        deadline=deadline,
        output_schema=inputs.output_schema,
        stop=stop,
        usage_observer=UsageObserver(source_time, usage.observe),
        on_message=messages.observe,
        before_start_write=require_running,
    )
    check_deadline()
    result = project_terminal_result(
        session.snapshot(), thread_id=thread_id, output_required=inputs.output_required
    )
    if result is None:
        raise TurnError(TurnFailure.INVALID_COMPLETION)
    payload = parse_json_object(result.data.model_dump(mode="json"))
    check_deadline()
    sink.emit("result", payload)
    return result
