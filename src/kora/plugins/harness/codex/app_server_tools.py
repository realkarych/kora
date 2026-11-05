from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from kora.plugins.harness.codex.app_server_messages import (
    MessageRefused,
    message_identity,
)
from kora.plugins.harness.codex.app_server_rpc import Notification

ToolKind = Literal["commandExecution", "fileChange"]
ToolPhase = Literal["started", "completed"]
ToolStatus = Literal["inProgress", "completed", "failed", "declined"]
CommandSource = Literal[
    "agent", "userShell", "unifiedExecStartup", "unifiedExecInteraction"
]


class ToolFailure(StrEnum):
    INVALID_OBSERVATION = "tool_invalid_observation"
    IDENTITY_MISMATCH = "tool_identity_mismatch"
    DUPLICATE = "tool_duplicate_observation"
    INVALID_TRANSITION = "tool_invalid_transition"
    ITEM_LIMIT = "tool_item_limit"
    PREMATURE = "tool_premature_observation"


class ToolRefused(Exception):
    def __init__(self, reason: ToolFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, repr=False)
class ToolObservation:
    thread_id: str
    turn_id: str
    item_id: str
    kind: ToolKind
    phase: ToolPhase
    status: ToolStatus
    source: CommandSource | None
    exit_code: int | None


@dataclass(frozen=True, repr=False)
class ToolCounts:
    items: int = 0
    started: int = 0
    completed: int = 0
    open: int = 0
    completion_without_start: int = 0


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ToolRefused(ToolFailure.INVALID_OBSERVATION)
    return cast("dict[str, object]", value)


def tool_identity(value: object) -> str:
    try:
        return message_identity(value)
    except MessageRefused:
        raise ToolRefused(ToolFailure.INVALID_OBSERVATION) from None


def decode_tool(notification: Notification) -> ToolObservation | None:
    if notification.method not in ("item/started", "item/completed"):
        return None
    params = object_fields(notification.params)
    item = object_fields(params.get("item"))
    kind = item.get("type")
    if not isinstance(kind, str) or not kind:
        raise ToolRefused(ToolFailure.INVALID_OBSERVATION)
    if kind not in ("commandExecution", "fileChange"):
        return None
    status = item.get("status")
    if not isinstance(status, str) or status not in (
        "inProgress",
        "completed",
        "failed",
        "declined",
    ):
        raise ToolRefused(ToolFailure.INVALID_OBSERVATION)
    source: CommandSource | None = None
    exit_code: int | None = None
    if kind == "commandExecution":
        if "source" in item:
            reported_source = item["source"]
            if not isinstance(reported_source, str) or reported_source not in (
                "agent",
                "userShell",
                "unifiedExecStartup",
                "unifiedExecInteraction",
            ):
                raise ToolRefused(ToolFailure.INVALID_OBSERVATION)
            source = reported_source
        reported_code = item.get("exitCode")
        if reported_code is not None and (
            type(reported_code) is not int or not -(2**31) <= reported_code <= 2**31 - 1
        ):
            raise ToolRefused(ToolFailure.INVALID_OBSERVATION)
        exit_code = reported_code
    return ToolObservation(
        tool_identity(params.get("threadId")),
        tool_identity(params.get("turnId")),
        tool_identity(item.get("id")),
        kind,
        "started" if notification.method == "item/started" else "completed",
        status,
        source,
        exit_code,
    )


def reject_premature_tool(notification: Notification) -> None:
    if decode_tool(notification) is not None:
        raise ToolRefused(ToolFailure.PREMATURE)


class TurnTools:
    def __init__(self, thread_id: str, *, max_tools: int) -> None:
        if type(max_tools) is not int or max_tools < 1:
            raise ValueError("invalid tool observation budget")
        self._thread_id: str = tool_identity(thread_id)
        self._max_tools: int = max_tools
        self._turn_id: str | None = None
        self._seen: dict[str, tuple[ToolKind, bool]] = {}
        self._latest: ToolObservation | None = None
        self._started: int = 0
        self._completed: int = 0
        self._open: int = 0
        self._completion_without_start: int = 0

    @property
    def counts(self) -> ToolCounts:
        return ToolCounts(
            len(self._seen),
            self._started,
            self._completed,
            self._open,
            self._completion_without_start,
        )

    def accept(self, notification: Notification) -> ToolObservation | None:
        observation = decode_tool(notification)
        if observation is None:
            return None
        if observation.thread_id != self._thread_id or (
            self._turn_id is not None and observation.turn_id != self._turn_id
        ):
            raise ToolRefused(ToolFailure.IDENTITY_MISMATCH)
        previous = self._seen.get(observation.item_id)
        if previous is None:
            if len(self._seen) == self._max_tools:
                raise ToolRefused(ToolFailure.ITEM_LIMIT)
        else:
            kind, completed = previous
            if kind != observation.kind or (
                completed and observation.phase == "started"
            ):
                raise ToolRefused(ToolFailure.INVALID_TRANSITION)
            if completed or observation.phase == "started":
                raise ToolRefused(ToolFailure.DUPLICATE)
        if observation.phase == "started":
            self._started += 1
            self._open += 1
        else:
            self._completed += 1
            if previous is None:
                self._completion_without_start += 1
            else:
                self._open -= 1
        self._seen[observation.item_id] = (
            observation.kind,
            observation.phase == "completed",
        )
        self._turn_id = observation.turn_id
        self._latest = observation
        return observation

    def for_turn(self, turn_id: str) -> ToolObservation | None:
        selected = tool_identity(turn_id)
        if self._turn_id is not None and self._turn_id != selected:
            raise ToolRefused(ToolFailure.IDENTITY_MISMATCH)
        return self._latest
