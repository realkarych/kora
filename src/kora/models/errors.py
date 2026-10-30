from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)
from pydantic_core import PydanticCustomError

from kora.ir.diagnostic_collection import (
    EMPTY_BYTES,
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTICS,
    DiagnosticCollection,
    copy_diagnostic,
    encoded_diagnostic,
)
from kora.ir.diagnostics import (
    MAX_INTEGER,
    Diagnostic,
    Index,
    Text1024,
    closed_fields,
)


class ApiErrorCode(StrEnum):
    NOT_FOUND = "API001"
    CONFLICT = "API002"
    VALIDATION = "API003"
    IDEMPOTENCY_MISMATCH = "API004"
    FORBIDDEN = "API005"
    APPROVAL_STALE = "API006"
    WEBHOOK_AUTHENTICATION = "API007"
    STALE_GRANT = "API008"
    UNAUTHENTICATED = "API009"
    INTERNAL = "API010"
    UNAVAILABLE = "API011"


class WireRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def require_known_fields(cls, value: object) -> object:
        return closed_fields(value, cls.model_fields)


class EmptyDetails(WireRecord):
    pass


class ConflictDetails(WireRecord):
    reason: Literal["lock_timeout"]


class ValidationDetails(WireRecord):
    errors: tuple[Diagnostic, ...] = Field(max_length=MAX_DIAGNOSTICS)
    warnings: tuple[Diagnostic, ...] = Field(max_length=MAX_DIAGNOSTICS)
    errors_omitted: Index
    warnings_omitted: Index

    @model_validator(mode="after")
    def require_collection_bounds(self) -> Self:
        count = (
            len(self.errors)
            + len(self.warnings)
            + self.errors_omitted
            + self.warnings_omitted
        )
        if count > MAX_INTEGER:
            raise PydanticCustomError(
                "diagnostic_count_limit",
                "Diagnostic count exceeds the supported range.",
            )
        size = EMPTY_BYTES
        for findings in (self.errors, self.warnings):
            size += max(0, len(findings) - 1)
            for finding in findings:
                size += len(encoded_diagnostic(copy_diagnostic(finding)))
                if size > MAX_DIAGNOSTIC_BYTES:
                    raise PydanticCustomError(
                        "diagnostic_byte_limit",
                        "Diagnostic data exceeds its byte limit.",
                    )
        return self

    @classmethod
    def from_collection(cls, collection: DiagnosticCollection) -> Self:
        if (
            len(collection.errors) > MAX_DIAGNOSTICS
            or len(collection.warnings) > MAX_DIAGNOSTICS
        ):
            raise ValueError("Diagnostic collection exceeds its count limit.")
        return cls(
            errors=tuple(copy_diagnostic(item) for item in collection.errors),
            warnings=tuple(copy_diagnostic(item) for item in collection.warnings),
            errors_omitted=collection.errors_omitted,
            warnings_omitted=collection.warnings_omitted,
        )

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.errors_omitted == 0


class ValidationResult(ValidationDetails):
    valid: StrictBool

    @model_validator(mode="after")
    def require_validity(self) -> Self:
        expected = not self.errors and self.errors_omitted == 0
        if self.valid != expected:
            raise PydanticCustomError(
                "diagnostic_validity", "Validation result contradicts its findings."
            )
        return self

    @classmethod
    def from_collection(cls, collection: DiagnosticCollection) -> Self:
        details = ValidationDetails.from_collection(collection)
        return cls(
            valid=details.is_valid,
            errors=details.errors,
            warnings=details.warnings,
            errors_omitted=details.errors_omitted,
            warnings_omitted=details.warnings_omitted,
        )


class ApiError(WireRecord):
    code: ApiErrorCode
    message: Text1024
    location: None = None
    retryable: StrictBool
    details: EmptyDetails | ConflictDetails | ValidationDetails
    request_id: StrictStr

    @model_validator(mode="after")
    def require_code_contract(self) -> Self:
        fixed_messages = {
            ApiErrorCode.UNAUTHENTICATED: "Authentication is required.",
            ApiErrorCode.INTERNAL: "Internal operation failure.",
            ApiErrorCode.UNAVAILABLE: "Required service is unavailable.",
        }
        fixed = fixed_messages.get(self.code)
        if fixed is not None and self.message != fixed:
            raise PydanticCustomError("api_message", "API error message is fixed.")
        if isinstance(self.details, ValidationDetails):
            if self.code != ApiErrorCode.VALIDATION:
                raise PydanticCustomError(
                    "api_details", "API error details are invalid."
                )
        elif isinstance(self.details, ConflictDetails):
            if self.code != ApiErrorCode.CONFLICT:
                raise PydanticCustomError(
                    "api_details", "API error details are invalid."
                )
        if self.retryable and self.code not in (
            ApiErrorCode.CONFLICT,
            ApiErrorCode.INTERNAL,
            ApiErrorCode.UNAVAILABLE,
        ):
            raise PydanticCustomError("api_retryable", "API error cannot be retried.")
        return self


class ApiErrorEnvelope(WireRecord):
    error: ApiError
