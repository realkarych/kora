from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import ValidationError

from kora.ir.diagnostic_collection import (
    DiagnosticCollection,
    DiagnosticCollectionError,
)
from kora.models.errors import (
    ApiError,
    ApiErrorCode,
    ApiErrorEnvelope,
    ConflictDetails,
    EmptyDetails,
    ValidationDetails,
    ValidationResult,
)
from kora.protocols.errors import KoraProtocolError

HTTP_STATUSES = MappingProxyType(
    {
        ApiErrorCode.NOT_FOUND: 404,
        ApiErrorCode.CONFLICT: 409,
        ApiErrorCode.VALIDATION: 422,
        ApiErrorCode.IDEMPOTENCY_MISMATCH: 409,
        ApiErrorCode.FORBIDDEN: 403,
        ApiErrorCode.APPROVAL_STALE: 409,
        ApiErrorCode.WEBHOOK_AUTHENTICATION: 401,
        ApiErrorCode.STALE_GRANT: 409,
        ApiErrorCode.UNAUTHENTICATED: 401,
        ApiErrorCode.INTERNAL: 500,
        ApiErrorCode.UNAVAILABLE: 503,
    }
)
MESSAGES = MappingProxyType(
    {
        ApiErrorCode.NOT_FOUND: "Object not found.",
        ApiErrorCode.CONFLICT: "Operation conflicts with current state.",
        ApiErrorCode.VALIDATION: "Request validation failed.",
        ApiErrorCode.IDEMPOTENCY_MISMATCH: (
            "Idempotency key does not match the request."
        ),
        ApiErrorCode.FORBIDDEN: "Operation is forbidden.",
        ApiErrorCode.APPROVAL_STALE: "Approval request is no longer current.",
        ApiErrorCode.WEBHOOK_AUTHENTICATION: "Webhook authentication failed.",
        ApiErrorCode.STALE_GRANT: "Execution grant is no longer current.",
        ApiErrorCode.UNAUTHENTICATED: "Authentication is required.",
        ApiErrorCode.INTERNAL: "Internal operation failure.",
        ApiErrorCode.UNAVAILABLE: "Required service is unavailable.",
    }
)


class InvalidCredential(StrEnum):
    MISSING = "missing"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    REVOKED = "revoked"
    COOKIE_ONLY = "cookie_only"


class RequestRejection(StrEnum):
    MALFORMED_JSON = "malformed_json"
    INVALID_ENCODING = "invalid_encoding"
    BYTE_LIMIT = "byte_limit"
    FRAMEWORK_SHAPE = "framework_shape"


class DependencyRetryContext(StrEnum):
    READ = "read"
    PROVEN_PRE_EFFECT = "proven_pre_effect"
    EXPLICIT_OPERATION_SAFE_RETRY = "explicit_operation_safe_retry"
    UNKNOWN_OUTCOME = "unknown_outcome"


@dataclass(frozen=True)
class ErrorMetadata:
    status: int
    headers: tuple[tuple[str, str], ...]


def error_metadata(envelope: ApiErrorEnvelope) -> ErrorMetadata:
    code = envelope.error.code
    return ErrorMetadata(
        status=HTTP_STATUSES[code],
        headers=(("WWW-Authenticate", "Bearer"),)
        if code == ApiErrorCode.UNAUTHENTICATED
        else (),
    )


def definitive_error(code: ApiErrorCode, *, request_id: str) -> ApiErrorEnvelope:
    if type(code) is not ApiErrorCode or code in (
        ApiErrorCode.INTERNAL,
        ApiErrorCode.UNAVAILABLE,
    ):
        raise ValueError("A definitive API error code is required.")
    return ApiErrorEnvelope(
        error=ApiError(
            code=code,
            message=MESSAGES[code],
            retryable=False,
            details=EmptyDetails(),
            request_id=request_id,
        )
    )


def unauthenticated_error(
    category: InvalidCredential, *, request_id: str
) -> ApiErrorEnvelope:
    if type(category) is not InvalidCredential:
        raise ValueError("An invalid credential category is required.")
    return definitive_error(ApiErrorCode.UNAUTHENTICATED, request_id=request_id)


def request_rejection(
    reason: RequestRejection | ValidationError, *, request_id: str
) -> ApiErrorEnvelope:
    if not isinstance(reason, ValidationError) and type(reason) is not RequestRejection:
        raise ValueError("A request rejection classification is required.")
    return definitive_error(ApiErrorCode.VALIDATION, request_id=request_id)


def internal_error(*, request_id: str) -> ApiErrorEnvelope:
    return ApiErrorEnvelope(
        error=ApiError(
            code=ApiErrorCode.INTERNAL,
            message=MESSAGES[ApiErrorCode.INTERNAL],
            retryable=False,
            details=EmptyDetails(),
            request_id=request_id,
        )
    )


def credential_audit_commit_failure(*, request_id: str) -> ApiErrorEnvelope:
    return ApiErrorEnvelope(
        error=ApiError(
            code=ApiErrorCode.INTERNAL,
            message=MESSAGES[ApiErrorCode.INTERNAL],
            retryable=True,
            details=EmptyDetails(),
            request_id=request_id,
        )
    )


def dependency_unavailable(
    context: DependencyRetryContext, *, request_id: str
) -> ApiErrorEnvelope:
    if type(context) is not DependencyRetryContext:
        raise ValueError("An explicit dependency retry context is required.")
    return ApiErrorEnvelope(
        error=ApiError(
            code=ApiErrorCode.UNAVAILABLE,
            message=MESSAGES[ApiErrorCode.UNAVAILABLE],
            retryable=context != DependencyRetryContext.UNKNOWN_OUTCOME,
            details=EmptyDetails(),
            request_id=request_id,
        )
    )


def validation_failure(
    failure: KoraProtocolError | DiagnosticCollectionError, *, request_id: str
) -> ApiErrorEnvelope:
    if type(failure) not in (KoraProtocolError, DiagnosticCollectionError):
        raise ValueError("A validation failure is required.")
    return internal_error(request_id=request_id)


def deployment_validation_error(
    collection: DiagnosticCollection, *, request_id: str
) -> ApiErrorEnvelope:
    details = ValidationDetails.from_collection(collection)
    if details.is_valid:
        raise ValueError("A deployment refusal requires an invalid collection.")
    return ApiErrorEnvelope(
        error=ApiError(
            code=ApiErrorCode.VALIDATION,
            message=MESSAGES[ApiErrorCode.VALIDATION],
            retryable=False,
            details=details,
            request_id=request_id,
        )
    )


def validation_result(collection: DiagnosticCollection) -> ValidationResult:
    return ValidationResult.from_collection(collection)


def retryable_conflict(
    reason: Literal["lock_timeout", "run_backlog_full"], *, request_id: str
) -> ApiErrorEnvelope:
    return ApiErrorEnvelope(
        error=ApiError(
            code=ApiErrorCode.CONFLICT,
            message=MESSAGES[ApiErrorCode.CONFLICT],
            retryable=True,
            details=ConflictDetails(reason=reason),
            request_id=request_id,
        )
    )
