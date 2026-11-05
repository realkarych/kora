from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from kora.plugins.harness.codex.app_server_rpc import Notification, rpc_integer

USAGE_METHOD = "thread/tokenUsage/updated"


class UsageFailure(StrEnum):
    INVALID_SNAPSHOT = "usage_invalid_snapshot"
    IDENTITY_MISMATCH = "usage_identity_mismatch"
    PREMATURE_SNAPSHOT = "usage_premature_snapshot"


class UsageError(Exception):
    def __init__(self, reason: UsageFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


class UsageChange(StrEnum):
    ADVANCED = "advanced"
    DUPLICATE = "duplicate"
    REGRESSION = "regression"


@dataclass(frozen=True, repr=False)
class TokenCounts:
    native_input_tokens: int
    cached_input_tokens: int
    cache_creation_tokens: int | None
    output_tokens: int
    reasoning_tokens: int

    @property
    def input_tokens(self) -> int | None:
        writes = self.cache_creation_tokens
        if writes is None:
            return None
        return self.native_input_tokens - self.cached_input_tokens - writes

    def includes(self, previous: "TokenCounts") -> bool:
        input_less_reads = self.native_input_tokens - self.cached_input_tokens
        previous_less_reads = (
            previous.native_input_tokens - previous.cached_input_tokens
        )
        if (
            self.cached_input_tokens < previous.cached_input_tokens
            or input_less_reads < previous_less_reads
            or self.reasoning_tokens < previous.reasoning_tokens
            or self.output_tokens - self.reasoning_tokens
            < previous.output_tokens - previous.reasoning_tokens
        ):
            return False
        if previous.cache_creation_tokens is None:
            return True
        writes = self.cache_creation_tokens
        return (
            writes is not None
            and writes >= previous.cache_creation_tokens
            and input_less_reads - writes
            >= previous_less_reads - previous.cache_creation_tokens
        )


@dataclass(frozen=True, repr=False)
class UsageSnapshot:
    thread_id: str
    turn_id: str
    counts: TokenCounts


@dataclass(frozen=True)
class ConfirmedUsage:
    counts: TokenCounts
    observed_at: datetime
    observation_gap: bool


@dataclass(frozen=True, repr=False)
class UsageObserver:
    source_time: Callable[[], datetime]
    observe: Callable[[ConfirmedUsage], None]


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UsageError(UsageFailure.INVALID_SNAPSHOT)
    return cast("dict[str, object]", value)


def usage_identifier(value: object) -> str:
    if isinstance(value, str) and value:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            pass
        else:
            return value
    raise UsageError(UsageFailure.INVALID_SNAPSHOT)


def token_count(value: object) -> int:
    if not rpc_integer(value):
        raise UsageError(UsageFailure.INVALID_SNAPSHOT)
    count = cast("int", value)
    if count < 0:
        raise UsageError(UsageFailure.INVALID_SNAPSHOT)
    return count


def decode_usage(notification: Notification) -> UsageSnapshot | None:
    if notification.method != USAGE_METHOD:
        return None
    params = object_fields(notification.params)
    thread_id = usage_identifier(params.get("threadId"))
    turn_id = usage_identifier(params.get("turnId"))
    usage = object_fields(params.get("tokenUsage"))
    total = object_fields(usage.get("total"))
    native_input = token_count(total.get("inputTokens"))
    reads = token_count(total.get("cachedInputTokens"))
    writes = (
        token_count(total["cacheWriteInputTokens"])
        if "cacheWriteInputTokens" in total
        else None
    )
    output = token_count(total.get("outputTokens"))
    reasoning = token_count(total.get("reasoningOutputTokens"))
    aggregate = token_count(total.get("totalTokens"))
    if (
        reads + (writes if writes is not None else 0) > native_input
        or reasoning > output
        or aggregate != native_input + output
    ):
        raise UsageError(UsageFailure.INVALID_SNAPSHOT)
    return UsageSnapshot(
        thread_id, turn_id, TokenCounts(native_input, reads, writes, output, reasoning)
    )


def reject_premature_usage(notification: Notification) -> None:
    if notification.method == USAGE_METHOD:
        raise UsageError(UsageFailure.PREMATURE_SNAPSHOT)


class TurnUsage:
    def __init__(self, thread_id: str) -> None:
        self._thread_id = usage_identifier(thread_id)
        self._latest: UsageSnapshot | None = None
        self._observation_gap = False

    @property
    def observation_gap(self) -> bool:
        return self._observation_gap

    def accept(self, snapshot: UsageSnapshot) -> UsageChange:
        previous = self._latest
        if snapshot.thread_id != self._thread_id or (
            previous is not None and snapshot.turn_id != previous.turn_id
        ):
            raise UsageError(UsageFailure.IDENTITY_MISMATCH)
        if previous is not None:
            if snapshot.counts == previous.counts:
                return UsageChange.DUPLICATE
            if not snapshot.counts.includes(previous.counts):
                self._observation_gap = True
                return UsageChange.REGRESSION
        self._latest = snapshot
        return UsageChange.ADVANCED

    def for_turn(self, turn_id: str) -> UsageSnapshot | None:
        latest = self._latest
        if latest is not None and latest.turn_id != turn_id:
            raise UsageError(UsageFailure.IDENTITY_MISMATCH)
        return latest
