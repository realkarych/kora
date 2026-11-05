from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from kora.plugins.harness.codex.app_server_rpc import Notification

ERROR_METHOD = "error"


class NativeErrorFailure(StrEnum):
    INVALID_DETAIL = "error_invalid_detail"
    INVALID_NOTIFICATION = "error_invalid_notification"
    IDENTITY_MISMATCH = "error_identity_mismatch"
    PREMATURE_ERROR = "error_premature_observation"


class NativeErrorRefused(Exception):
    def __init__(self, reason: NativeErrorFailure) -> None:
        self.reason: NativeErrorFailure = reason
        super().__init__(reason.value)


class NativeErrorCode(StrEnum):
    CONTEXT_WINDOW_EXCEEDED = "contextWindowExceeded"
    SESSION_BUDGET_EXCEEDED = "sessionBudgetExceeded"
    USAGE_LIMIT_EXCEEDED = "usageLimitExceeded"
    RATE_LIMIT_EXCEEDED = "rateLimitExceeded"
    FLEX_UNAVAILABLE = "flexUnavailable"
    SERVER_OVERLOADED = "serverOverloaded"
    MODEL_NOT_FOUND = "modelNotFound"
    POLICY_CAPACITY_UNAVAILABLE = "policyCapacityUnavailable"
    CYBER_POLICY = "cyberPolicy"
    MISALIGNMENT_POLICY_VIOLATION = "misalignmentPolicyViolation"
    TOO_MANY_DENIALS = "tooManyDenials"
    INTERNAL_SERVER_ERROR = "internalServerError"
    UNAUTHORIZED = "unauthorized"
    BAD_REQUEST = "badRequest"
    THREAD_ROLLBACK_FAILED = "threadRollbackFailed"
    SANDBOX_ERROR = "sandboxError"
    OTHER = "other"
    HTTP_CONNECTION_FAILED = "httpConnectionFailed"
    RESPONSE_STREAM_CONNECTION_FAILED = "responseStreamConnectionFailed"
    RESPONSE_STREAM_DISCONNECTED = "responseStreamDisconnected"
    RESPONSE_TOO_MANY_FAILED_ATTEMPTS = "responseTooManyFailedAttempts"
    ACTIVE_TURN_NOT_STEERABLE = "activeTurnNotSteerable"
    UNSPECIFIED = "unspecified"
    UNRECOGNIZED = "unrecognized"


HTTP_ERROR_CODES: frozenset[NativeErrorCode] = frozenset(
    {
        NativeErrorCode.HTTP_CONNECTION_FAILED,
        NativeErrorCode.RESPONSE_STREAM_CONNECTION_FAILED,
        NativeErrorCode.RESPONSE_STREAM_DISCONNECTED,
        NativeErrorCode.RESPONSE_TOO_MANY_FAILED_ATTEMPTS,
    }
)
UNKNOWN_CODES: frozenset[NativeErrorCode] = frozenset(
    {NativeErrorCode.UNSPECIFIED, NativeErrorCode.UNRECOGNIZED}
)


@dataclass(frozen=True, repr=False)
class NativeErrorInfo:
    code: NativeErrorCode
    http_status_code: int | None = None
    turn_kind: Literal["review", "compact"] | None = None


@dataclass(frozen=True, repr=False)
class ErrorObservation:
    thread_id: str
    turn_id: str
    will_retry: bool
    info: NativeErrorInfo


def object_fields(value: object, reason: NativeErrorFailure) -> dict[str, object]:
    if not isinstance(value, dict):
        raise NativeErrorRefused(reason)
    return cast("dict[str, object]", value)


def error_identifier(value: object) -> str:
    if isinstance(value, str) and value:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            pass
        else:
            return value
    raise NativeErrorRefused(NativeErrorFailure.INVALID_NOTIFICATION)


def native_code(value: str) -> NativeErrorCode:
    try:
        code = NativeErrorCode(value)
    except ValueError:
        return NativeErrorCode.UNRECOGNIZED
    return NativeErrorCode.UNRECOGNIZED if code in UNKNOWN_CODES else code


def decode_error_info(value: object) -> NativeErrorInfo:
    reason = NativeErrorFailure.INVALID_DETAIL
    fields = object_fields(value, reason)
    if not isinstance(fields.get("message"), str):
        raise NativeErrorRefused(reason)
    detail = fields.get("codexErrorInfo")
    if detail is None:
        return NativeErrorInfo(NativeErrorCode.UNSPECIFIED)
    if isinstance(detail, str):
        code = native_code(detail)
        if (
            code in HTTP_ERROR_CODES
            or code is NativeErrorCode.ACTIVE_TURN_NOT_STEERABLE
        ):
            raise NativeErrorRefused(reason)
        return NativeErrorInfo(code)
    variants = object_fields(detail, reason)
    if len(variants) != 1:
        raise NativeErrorRefused(reason)
    name = next(iter(variants))
    code = native_code(name)
    if code is NativeErrorCode.UNRECOGNIZED:
        return NativeErrorInfo(code)
    if (
        code not in HTTP_ERROR_CODES
        and code is not NativeErrorCode.ACTIVE_TURN_NOT_STEERABLE
    ):
        raise NativeErrorRefused(reason)
    payload = object_fields(variants[name], reason)
    if code is NativeErrorCode.ACTIVE_TURN_NOT_STEERABLE:
        turn_kind = payload.get("turnKind")
        if turn_kind not in ("review", "compact"):
            raise NativeErrorRefused(reason)
        return NativeErrorInfo(code, turn_kind=turn_kind)
    status = payload.get("httpStatusCode")
    if status is not None and (type(status) is not int or not 0 <= status <= 65_535):
        raise NativeErrorRefused(reason)
    return NativeErrorInfo(code, http_status_code=status)


def decode_error(notification: Notification) -> ErrorObservation | None:
    if notification.method != ERROR_METHOD:
        return None
    params = object_fields(notification.params, NativeErrorFailure.INVALID_NOTIFICATION)
    thread_id = error_identifier(params.get("threadId"))
    turn_id = error_identifier(params.get("turnId"))
    will_retry = params.get("willRetry")
    if type(will_retry) is not bool:
        raise NativeErrorRefused(NativeErrorFailure.INVALID_NOTIFICATION)
    return ErrorObservation(
        thread_id, turn_id, will_retry, decode_error_info(params.get("error"))
    )


def reject_premature_error(notification: Notification) -> None:
    if notification.method == ERROR_METHOD:
        raise NativeErrorRefused(NativeErrorFailure.PREMATURE_ERROR)


class TurnErrors:
    def __init__(self, thread_id: str) -> None:
        self._thread_id: str = error_identifier(thread_id)
        self._latest: ErrorObservation | None = None

    def accept(self, notification: Notification) -> ErrorObservation | None:
        observation = decode_error(notification)
        if observation is None:
            return None
        previous = self._latest
        if observation.thread_id != self._thread_id or (
            previous is not None and observation.turn_id != previous.turn_id
        ):
            raise NativeErrorRefused(NativeErrorFailure.IDENTITY_MISMATCH)
        self._latest = observation
        return observation

    def for_turn(self, turn_id: str) -> ErrorObservation | None:
        error_identifier(turn_id)
        latest = self._latest
        if latest is not None and latest.turn_id != turn_id:
            raise NativeErrorRefused(NativeErrorFailure.IDENTITY_MISMATCH)
        return latest
