import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from kora.plugins.harness.codex.app_server_errors import (
    ErrorObservation,
    NativeErrorInfo,
    TurnErrors,
    decode_error_info,
    reject_premature_error,
)
from kora.plugins.harness.codex.app_server_messages import (
    CompletedMessage,
    TurnMessages,
    reject_premature_message,
)
from kora.plugins.harness.codex.app_server_rpc import AppServerRpc, Notification
from kora.plugins.harness.codex.app_server_tools import (
    ToolCounts,
    ToolObservation,
    TurnTools,
    reject_premature_tool,
)
from kora.plugins.harness.codex.app_server_turn import (
    MAX_FINAL_MESSAGE_BYTES,
    TerminalStatus,
    TurnCompletion,
    observe_app_server_turn,
)
from kora.plugins.harness.codex.app_server_usage import (
    ConfirmedUsage,
    TurnUsage,
    UsageChange,
    UsageObserver,
    UsageSnapshot,
    decode_usage,
    reject_premature_usage,
)


class SessionFailure(StrEnum):
    ALREADY_USED = "session_already_used"
    PENDING_MESSAGE_LIMIT = "session_pending_message_limit"


class SessionRefused(Exception):
    def __init__(self, reason: SessionFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, repr=False)
class TurnObservations:
    turn_id: str | None = None
    usage: UsageSnapshot | None = None
    usage_gap: bool = False
    message_count: int = 0
    latest_message: CompletedMessage | None = None
    latest_error: ErrorObservation | None = None
    terminal_status: TerminalStatus | None = None
    final_message: CompletedMessage | None = None
    terminal_error: NativeErrorInfo | None = None
    tool_counts: ToolCounts = ToolCounts()
    latest_tool: ToolObservation | None = None


class AppServerTurnSession:
    def __init__(self, thread_id: str, *, max_messages: int, max_tools: int) -> None:
        self._thread_id = thread_id
        self._messages = TurnMessages(
            thread_id,
            max_messages=max_messages,
            max_text_bytes=MAX_FINAL_MESSAGE_BYTES,
        )
        self._usage = TurnUsage(thread_id)
        self._errors = TurnErrors(thread_id)
        self._tools = TurnTools(thread_id, max_tools=max_tools)
        self._used = False
        self._snapshot = TurnObservations()

    def snapshot(self) -> TurnObservations:
        return self._snapshot

    def _confirmed(
        self, turn_id: str, completion: TurnCompletion | None = None
    ) -> TurnObservations:
        usage = self._usage.for_turn(turn_id)
        message = self._messages.for_turn(turn_id)
        error = self._errors.for_turn(turn_id)
        tool = self._tools.for_turn(turn_id)
        terminal_error = (
            decode_error_info(completion.error)
            if completion is not None and completion.error is not None
            else None
        )
        return TurnObservations(
            turn_id=turn_id,
            usage=usage,
            usage_gap=self._usage.observation_gap,
            message_count=self._messages.count,
            latest_message=message,
            latest_error=error,
            terminal_status=completion.status if completion is not None else None,
            final_message=completion.final_message if completion is not None else None,
            terminal_error=terminal_error,
            tool_counts=self._tools.counts,
            latest_tool=tool,
        )

    async def run(
        self,
        rpc: AppServerRpc,
        *,
        prompt: str,
        deadline: float,
        output_schema: dict[str, object] | None = None,
        stop: asyncio.Event | None = None,
        observe: Callable[[Notification], bool | None] | None = None,
        on_started: Callable[[str], None] | None = None,
        on_waiting: Callable[[], None] | None = None,
        usage_observer: UsageObserver | None = None,
        on_message: Callable[[int, CompletedMessage], None] | None = None,
        before_start_write: Callable[[], None] | None = None,
    ) -> TurnCompletion:
        if self._used:
            raise SessionRefused(SessionFailure.ALREADY_USED)
        self._used = True
        source_time: datetime | None = None
        usage_pending = False
        pending_messages: list[CompletedMessage] = []
        pending_bytes = 0

        def publish_message(ordinal: int, message: CompletedMessage) -> None:
            if on_message is not None:
                rpc.check_deadline(deadline)
                on_message(ordinal, message)
                rpc.check_deadline(deadline)

        def publish_usage() -> None:
            nonlocal usage_pending
            if not usage_pending or usage_observer is None:
                return
            snapshot = self._snapshot
            if snapshot.usage is not None and source_time is not None:
                usage_pending = False
                usage_observer.observe(
                    ConfirmedUsage(
                        snapshot.usage.counts, source_time, snapshot.usage_gap
                    )
                )

        def accept(notification: Notification) -> bool | None:
            nonlocal source_time, usage_pending, pending_bytes
            usage = decode_usage(notification)
            if usage is not None:
                previous_gap = self._usage.observation_gap
                change = self._usage.accept(usage)
                if usage_observer is not None:
                    if change is UsageChange.ADVANCED:
                        source_time = usage_observer.source_time()
                        usage_pending = True
                    elif self._usage.observation_gap != previous_gap:
                        usage_pending = True
            message = self._messages.accept(notification)
            self._errors.accept(notification)
            self._tools.accept(notification)
            turn_id = self._snapshot.turn_id
            if turn_id is not None:
                self._snapshot = self._confirmed(turn_id)
                if message is not None:
                    publish_message(self._snapshot.message_count, message)
                publish_usage()
            elif message is not None and on_message is not None:
                pending_bytes += sum(
                    len(value.encode("utf-8"))
                    for value in (
                        message.thread_id,
                        message.turn_id,
                        message.message_id,
                        message.text,
                    )
                )
                if pending_bytes > MAX_FINAL_MESSAGE_BYTES:
                    raise SessionRefused(SessionFailure.PENDING_MESSAGE_LIMIT)
                pending_messages.append(message)
            return observe(notification) if observe is not None else None

        def reject_premature(notification: Notification) -> bool | None:
            reject_premature_usage(notification)
            reject_premature_message(notification)
            reject_premature_error(notification)
            reject_premature_tool(notification)
            return observe(notification) if observe is not None else None

        def started(turn_id: str) -> None:
            self._snapshot = self._confirmed(turn_id)
            if on_started is not None:
                on_started(turn_id)
            for ordinal, message in enumerate(pending_messages, 1):
                publish_message(ordinal, message)
            pending_messages.clear()
            publish_usage()

        try:
            completion = await observe_app_server_turn(
                rpc,
                thread_id=self._thread_id,
                prompt=prompt,
                deadline=deadline,
                output_schema=output_schema,
                stop=stop,
                observe=accept,
                observe_before_send=reject_premature,
                on_started=started,
                on_waiting=on_waiting,
                before_start_write=before_start_write,
            )
        finally:
            pending_messages.clear()
        self._snapshot = self._confirmed(completion.turn_id, completion)
        return completion
