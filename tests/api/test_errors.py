import json
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from kora.api.errors import (
    HTTP_STATUSES,
    DependencyRetryContext,
    InvalidCredential,
    RequestRejection,
    credential_audit_commit_failure,
    definitive_error,
    dependency_unavailable,
    deployment_validation_error,
    error_metadata,
    internal_error,
    lock_timeout_error,
    request_rejection,
    unauthenticated_error,
    validation_failure,
    validation_result,
)
from kora.ir.diagnostic_collection import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTICS,
    DiagnosticCollection,
    DiagnosticCollectionError,
    DiagnosticCollector,
    encoded_diagnostic,
    increment_count,
)
from kora.ir.diagnostics import MAX_INTEGER, Diagnostic, SourceLocation
from kora.models.errors import (
    ApiError,
    ApiErrorCode,
    ApiErrorEnvelope,
    EmptyDetails,
    ValidationDetails,
    ValidationResult,
)
from kora.protocols.errors import KoraProtocolError

REQUEST_ID = "req_saved_error_identity"
POISON = "private_exception_prose_marker"
INPUT_POISON = "private_rejected_input_marker"
KEY_POISON = "private_authored_key_marker"


class ProviderFault(KoraProtocolError):
    pass


class CollectionFault(DiagnosticCollectionError):
    pass


class PoisonedRequest(BaseModel):
    value: int
    mapping: dict[str, int]

    @field_validator("value")
    @classmethod
    def reject(cls, value: int) -> int:
        raise ValueError(POISON)


def poisoned_validation_error() -> ValidationError:
    try:
        PoisonedRequest.model_validate(
            {"value": 1, "mapping": {KEY_POISON: INPUT_POISON}}
        )
    except ValidationError as error:
        return error
    raise AssertionError("Expected a real validation error.")


def collected() -> DiagnosticCollection:
    collector = DiagnosticCollector()
    collector.add(
        Diagnostic(
            code="IR011",
            message="Expression is invalid.",
            path=("graph", "nodes", "review", "when"),
            location=SourceLocation(file="graphs/review.py", line=3),
            offset=2,
            node="review",
            hint="Check the expression.",
        )
    )
    collector.add(
        Diagnostic(code="Provider.future", message="Configuration warning."), "warning"
    )
    return collector.finish()


def test_every_error_code_has_one_status() -> None:
    expected = {
        "API001": 404,
        "API002": 409,
        "API003": 422,
        "API004": 409,
        "API005": 403,
        "API006": 409,
        "API007": 401,
        "API008": 409,
        "API009": 401,
        "API010": 500,
        "API011": 503,
    }

    actual = {code.value: status for code, status in HTTP_STATUSES.items()}

    assert actual == expected
    assert set(HTTP_STATUSES) == set(ApiErrorCode)


@pytest.mark.parametrize("category", list(InvalidCredential))
def test_unauthenticated_categories_have_uniform_api009(
    category: InvalidCredential,
) -> None:
    request_id = f"req_{category.value}"

    envelope = unauthenticated_error(category, request_id=request_id)
    metadata = error_metadata(envelope)

    assert envelope.model_dump(mode="json") == {
        "error": {
            "code": "API009",
            "message": "Authentication is required.",
            "location": None,
            "retryable": False,
            "details": {},
            "request_id": request_id,
        }
    }
    assert metadata.status == 401
    assert metadata.headers == (("WWW-Authenticate", "Bearer"),)


@pytest.mark.parametrize(
    "code",
    [
        code
        for code in ApiErrorCode
        if code not in (ApiErrorCode.INTERNAL, ApiErrorCode.UNAVAILABLE)
    ],
)
def test_definitive_errors_are_nonretryable(code: ApiErrorCode) -> None:
    envelope = definitive_error(code, request_id=REQUEST_ID)

    metadata = error_metadata(envelope)

    assert metadata.status == HTTP_STATUSES[code]
    assert envelope.error.retryable is False
    assert envelope.error.location is None
    assert envelope.error.details == EmptyDetails()
    assert metadata.headers == (
        (("WWW-Authenticate", "Bearer"),)
        if code == ApiErrorCode.UNAUTHENTICATED
        else ()
    )


@pytest.mark.parametrize("reason", list(RequestRejection))
def test_request_rejection_is_content_free(reason: RequestRejection) -> None:
    envelope = request_rejection(reason, request_id=REQUEST_ID)

    body = envelope.model_dump(mode="json")

    assert body["error"]["code"] == "API003"
    assert body["error"]["details"] == {}
    assert error_metadata(envelope).status == 422
    assert envelope.error.retryable is False


def test_real_validation_errors_never_expose_input_prose_keys_or_context() -> None:
    failure = poisoned_validation_error()
    assert POISON in str(failure.errors())
    assert INPUT_POISON in str(failure.errors())
    assert KEY_POISON in str(failure.errors())

    envelope = request_rejection(failure, request_id=REQUEST_ID)
    rendered = envelope.model_dump_json()

    assert POISON not in rendered
    assert INPUT_POISON not in rendered
    assert KEY_POISON not in rendered
    assert "ValueError" not in rendered
    assert "ctx" not in rendered
    assert envelope.error.code == ApiErrorCode.VALIDATION
    assert envelope.error.message == "Request validation failed."
    assert envelope.error.details == EmptyDetails()


@pytest.mark.parametrize("code", ["diagnostics_limit", "unexpected_provider_fault"])
def test_real_provider_errors_cannot_leak_or_authorize_retry(code: str) -> None:
    failure = KoraProtocolError(code, POISON, True, {POISON: POISON})

    envelope = validation_failure(failure, request_id=REQUEST_ID)

    assert POISON not in envelope.model_dump_json()
    assert envelope == internal_error(request_id=REQUEST_ID)
    assert envelope.error.retryable is False
    assert error_metadata(envelope).status == 500


def test_count_overflow_discards_incomplete_validation() -> None:
    with pytest.raises(DiagnosticCollectionError) as caught:
        increment_count(MAX_INTEGER)

    envelope = validation_failure(caught.value, request_id=REQUEST_ID)

    assert envelope == internal_error(request_id=REQUEST_ID)
    assert envelope.error.retryable is False
    assert envelope.error.details == EmptyDetails()


def test_validation_and_deployment_preserve_completed_diagnostics() -> None:
    collection = collected()

    result = validation_result(collection)
    envelope = deployment_validation_error(collection, request_id=REQUEST_ID)
    detail_body = cast(
        "dict[str, object]", envelope.model_dump(mode="json")["error"]["details"]
    )

    assert result.valid is False
    assert result.errors == collection.errors
    assert result.warnings == collection.warnings
    assert result.errors[0] is not collection.errors[0]
    assert detail_body == {
        key: value
        for key, value in result.model_dump(mode="json").items()
        if key != "valid"
    }
    assert detail_body["errors_omitted"] == 0
    assert detail_body["warnings_omitted"] == 0
    assert result.errors[0].offset == 2
    assert result.warnings[0].code == "Provider.future"
    assert result.warnings[0].path is None


def test_omitted_errors_invalidate_an_empty_retained_collection() -> None:
    collection = DiagnosticCollection((), (), 1, 7)

    result = validation_result(collection)
    envelope = deployment_validation_error(collection, request_id=REQUEST_ID)

    assert result.valid is False
    assert result.errors_omitted == 1
    assert result.warnings_omitted == 7
    assert envelope.error.details == ValidationDetails(
        errors=(), warnings=(), errors_omitted=1, warnings_omitted=7
    )


def test_warning_only_validation_succeeds_and_cannot_refuse_deployment() -> None:
    collector = DiagnosticCollector()
    collector.add(Diagnostic(code="IR009", message="Configuration warning."), "warning")
    collection = collector.finish()

    result = validation_result(collection)

    assert result.valid is True
    with pytest.raises(ValueError, match="requires an invalid collection"):
        deployment_validation_error(collection, request_id=REQUEST_ID)


@pytest.mark.parametrize("omitted", [-1, MAX_INTEGER + 1, True])
def test_invalid_omission_counts_are_refused(omitted: int) -> None:
    collection = DiagnosticCollection((), (), omitted, 0)

    with pytest.raises(ValidationError):
        validation_result(collection)


def test_aggregate_count_overflow_is_refused_at_wire_boundary() -> None:
    collection = DiagnosticCollection((), (), MAX_INTEGER, 1)

    with pytest.raises(ValidationError, match="supported range"):
        validation_result(collection)


def test_oversized_collection_is_refused_before_copying() -> None:
    finding = Diagnostic(code="IR003", message="Invalid declaration.")
    collection = DiagnosticCollection((finding,) * (MAX_DIAGNOSTICS + 1), (), 0, 0)

    with pytest.raises(ValueError, match="count limit"):
        validation_result(collection)


def test_diagnostic_byte_limit_is_enforced_for_manually_built_collection() -> None:
    finding = Diagnostic(code="IR003", message="x" * 1024)
    count = MAX_DIAGNOSTIC_BYTES // len(encoded_diagnostic(finding)) + 1
    collection = DiagnosticCollection((finding,) * count, (), 0, 0)

    with pytest.raises(ValidationError, match="byte limit"):
        validation_result(collection)


@pytest.mark.parametrize("context", list(DependencyRetryContext))
def test_dependency_retry_requires_explicit_operation_context(
    context: DependencyRetryContext,
) -> None:
    envelope = dependency_unavailable(context, request_id=REQUEST_ID)

    assert envelope.error.code == ApiErrorCode.UNAVAILABLE
    assert envelope.error.retryable == (
        context != DependencyRetryContext.UNKNOWN_OUTCOME
    )
    assert error_metadata(envelope).status == 503
    assert envelope.error.message == "Required service is unavailable."
    assert envelope.error.details == EmptyDetails()


def test_retry_exceptions_are_distinct() -> None:
    ordinary = internal_error(request_id=REQUEST_ID)

    audit_failure = credential_audit_commit_failure(request_id=REQUEST_ID)
    lock_failure = lock_timeout_error(request_id=REQUEST_ID)

    assert ordinary.error.retryable is False
    assert audit_failure.error.retryable is True
    assert audit_failure.error.code == ApiErrorCode.INTERNAL
    assert audit_failure.error.message == ordinary.error.message
    assert audit_failure.error.details == ordinary.error.details
    assert error_metadata(audit_failure).status == 500
    assert error_metadata(lock_failure).status == 409


@pytest.mark.parametrize(
    "code",
    [ApiErrorCode.UNAUTHENTICATED, ApiErrorCode.INTERNAL, ApiErrorCode.UNAVAILABLE],
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message", POISON),
        ("details", {POISON: POISON}),
        ("location", POISON),
        (POISON, POISON),
    ],
)
def test_fixed_error_envelopes_reject_arbitrary_fields(
    code: ApiErrorCode, field: str, value: object
) -> None:
    envelope = (
        internal_error(request_id=REQUEST_ID)
        if code == ApiErrorCode.INTERNAL
        else dependency_unavailable(
            DependencyRetryContext.UNKNOWN_OUTCOME, request_id=REQUEST_ID
        )
        if code == ApiErrorCode.UNAVAILABLE
        else unauthenticated_error(InvalidCredential.MISSING, request_id=REQUEST_ID)
    )
    body = envelope.model_dump(mode="json")
    body["error"][field] = value

    with pytest.raises(ValidationError):
        ApiErrorEnvelope.model_validate(body)


def test_wire_models_reject_extra_envelope_fields_and_inconsistent_validity() -> None:
    body = internal_error(request_id=REQUEST_ID).model_dump(mode="json")
    body[POISON] = POISON

    with pytest.raises(ValidationError):
        ApiErrorEnvelope.model_validate(body)
    with pytest.raises(ValidationError, match="contradicts"):
        ValidationResult(
            valid=True, errors=(), warnings=(), errors_omitted=1, warnings_omitted=0
        )


def test_saved_error_id_survives_wire_round_trip() -> None:
    saved = deployment_validation_error(collected(), request_id=REQUEST_ID)
    saved_body = saved.model_dump_json()

    replayed = ApiErrorEnvelope.model_validate_json(saved_body)

    assert replayed.error.request_id == REQUEST_ID
    assert replayed.model_dump_json() == saved_body
    assert json.loads(saved_body)["error"]["request_id"] == REQUEST_ID


@pytest.mark.parametrize("code", [ApiErrorCode.INTERNAL, ApiErrorCode.UNAVAILABLE])
def test_definitive_builder_cannot_bypass_failure_classification(
    code: ApiErrorCode,
) -> None:
    with pytest.raises(ValueError, match="definitive"):
        definitive_error(code, request_id=REQUEST_ID)


def test_nonretryable_codes_refuse_a_retryable_wire_value() -> None:
    body = definitive_error(
        ApiErrorCode.FORBIDDEN, request_id=REQUEST_ID
    ).error.model_dump(mode="json")
    body["retryable"] = True

    with pytest.raises(ValidationError, match="cannot be retried"):
        ApiError.model_validate(body)


@pytest.mark.parametrize(
    "failure",
    [
        ProviderFault("diagnostics_limit", POISON, True, {KEY_POISON: INPUT_POISON}),
        CollectionFault(),
    ],
)
def test_validation_failure_accepts_inherited_exception_families(
    failure: KoraProtocolError | DiagnosticCollectionError,
) -> None:
    request_id = "req_inherited_failure"

    envelope = validation_failure(failure, request_id=request_id)

    assert envelope == internal_error(request_id=request_id)
    assert envelope.error.retryable is False
    assert envelope.error.request_id == request_id
    assert POISON not in envelope.model_dump_json()
    assert KEY_POISON not in envelope.model_dump_json()
    assert INPUT_POISON not in envelope.model_dump_json()


@pytest.mark.parametrize(
    "failure",
    [None, RuntimeError(POISON), POISON, {"code": "diagnostics_limit"}],
)
def test_validation_failure_refuses_objects_outside_accepted_exception_families(
    failure: object,
) -> None:
    with pytest.raises(
        ValueError, match=r"^A validation failure is required\.$"
    ) as caught:
        validation_failure(failure, request_id=REQUEST_ID)

    assert POISON not in str(caught.value)
    assert KEY_POISON not in str(caught.value)
    assert INPUT_POISON not in str(caught.value)
