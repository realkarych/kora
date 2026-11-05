from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from kora.plugins.harness.codex.app_server_rpc import Notification

COMPLETED_ITEM_METHOD = "item/completed"
MAX_IDENTITY_BYTES = 256
MessagePhase = Literal["commentary", "partial_answer", "final_answer"]


class MessageFailure(StrEnum):
    INVALID_MESSAGE = "message_invalid_observation"
    IDENTITY_MISMATCH = "message_identity_mismatch"
    DUPLICATE_MESSAGE = "message_duplicate_observation"
    MESSAGE_LIMIT = "message_count_limit"
    TEXT_LIMIT = "message_text_limit"
    PREMATURE_MESSAGE = "message_premature_observation"


class MessageRefused(Exception):
    def __init__(self, reason: MessageFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, repr=False)
class CompletedMessage:
    thread_id: str
    turn_id: str
    message_id: str
    text: str
    phase: MessagePhase | None


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MessageRefused(MessageFailure.INVALID_MESSAGE)
    return cast("dict[str, object]", value)


def bounded_text(value: object, limit: int, failure: MessageFailure) -> str:
    if not isinstance(value, str):
        raise MessageRefused(MessageFailure.INVALID_MESSAGE)
    if len(value) > limit:
        raise MessageRefused(failure)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise MessageRefused(MessageFailure.INVALID_MESSAGE) from None
    if size > limit:
        raise MessageRefused(failure)
    return value


def message_identity(value: object) -> str:
    identity = bounded_text(value, MAX_IDENTITY_BYTES, MessageFailure.INVALID_MESSAGE)
    if not identity:
        raise MessageRefused(MessageFailure.INVALID_MESSAGE)
    return identity


def completed_item(notification: Notification) -> dict[str, object] | None:
    if notification.method != COMPLETED_ITEM_METHOD:
        return None
    item = object_fields(object_fields(notification.params).get("item"))
    kind = item.get("type")
    if not isinstance(kind, str) or not kind:
        raise MessageRefused(MessageFailure.INVALID_MESSAGE)
    return item if kind == "agentMessage" else None


def decode_completed_message(
    notification: Notification, *, max_text_bytes: int
) -> CompletedMessage | None:
    if type(max_text_bytes) is not int or max_text_bytes < 1:
        raise ValueError("invalid completed-message budget")
    item = completed_item(notification)
    if item is None:
        return None
    params = object_fields(notification.params)
    phase = item.get("phase")
    if phase is not None and phase not in (
        "commentary",
        "partial_answer",
        "final_answer",
    ):
        raise MessageRefused(MessageFailure.INVALID_MESSAGE)
    return CompletedMessage(
        message_identity(params.get("threadId")),
        message_identity(params.get("turnId")),
        message_identity(item.get("id")),
        bounded_text(item.get("text"), max_text_bytes, MessageFailure.TEXT_LIMIT),
        phase,
    )


def reject_premature_message(notification: Notification) -> None:
    if completed_item(notification) is not None:
        raise MessageRefused(MessageFailure.PREMATURE_MESSAGE)


class TurnMessages:
    def __init__(
        self, thread_id: str, *, max_messages: int, max_text_bytes: int
    ) -> None:
        if (
            type(max_messages) is not int
            or max_messages < 1
            or type(max_text_bytes) is not int
            or max_text_bytes < 1
        ):
            raise ValueError("invalid completed-message budget")
        self._thread_id = message_identity(thread_id)
        self._max_messages = max_messages
        self._max_text_bytes = max_text_bytes
        self._message_ids: set[str] = set()
        self._latest: CompletedMessage | None = None

    @property
    def count(self) -> int:
        return len(self._message_ids)

    def accept(self, notification: Notification) -> CompletedMessage | None:
        observation = decode_completed_message(
            notification, max_text_bytes=self._max_text_bytes
        )
        if observation is None:
            return None
        previous = self._latest
        if observation.thread_id != self._thread_id or (
            previous is not None and observation.turn_id != previous.turn_id
        ):
            raise MessageRefused(MessageFailure.IDENTITY_MISMATCH)
        if observation.message_id in self._message_ids:
            raise MessageRefused(MessageFailure.DUPLICATE_MESSAGE)
        if self.count == self._max_messages:
            raise MessageRefused(MessageFailure.MESSAGE_LIMIT)
        self._message_ids.add(observation.message_id)
        self._latest = observation
        return observation

    def for_turn(self, turn_id: str) -> CompletedMessage | None:
        message_identity(turn_id)
        latest = self._latest
        if latest is not None and latest.turn_id != turn_id:
            raise MessageRefused(MessageFailure.IDENTITY_MISMATCH)
        return latest
