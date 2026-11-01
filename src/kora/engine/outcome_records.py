import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal, Never, TypeAlias

from pydantic import Field, StrictInt, ValidationError, model_validator

from kora.db.types import JsonValue
from kora.ir import ErrorClass
from kora.ir.json import (
    DEFAULT_MAX_DEPTH,
    JsonObjectField,
    JsonShapeError,
    fits_utf8,
    parse_json_object,
)
from kora.ir.vcs_ref_json import MAX_JSON_BYTES, canonical_bytes
from kora.protocols.boundary import BoundaryModel
from kora.protocols.evidence import MAX_ENVELOPE_BYTES, Code
from kora.protocols.identity import GrantBinding, Identifier, Sha256
from kora.protocols.report import (
    MAX_ERROR_BYTES,
    AttemptError,
    LostReason,
    TerminalReport,
)

REPLAY_FORMAT: str = "attempt-report-replay/1"
MAX_ERROR_PAYLOAD_BYTES: int = MAX_ERROR_BYTES + 1024
MAX_RECEIPT_BYTES: int = 16_384
MAX_REPORT_BYTES: int = MAX_JSON_BYTES + MAX_ENVELOPE_BYTES + MAX_ERROR_BYTES + 65_536
MAX_REPORT_DEPTH: int = DEFAULT_MAX_DEPTH + 8

DesiredState: TypeAlias = Literal["run", "cancel"]
ReportOutcome: TypeAlias = Literal["succeeded", "failed", "cancelled", "lost"]
PayloadState: TypeAlias = Literal[
    "not_applicable", "available", "expiring", "expired", "missing"
]
UnavailableState: TypeAlias = Literal["expired", "missing"]


class OutcomeRecordRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("the original outcome record is invalid")


def refuse() -> Never:
    raise OutcomeRecordRejected()


class AcceptedResponse(BoundaryModel):
    status: Literal["finishing"]
    desired_state: DesiredState


class ErrorPayloadDescriptor(BoundaryModel):
    format: Literal["attempt-error/1"]
    sha256: Sha256
    size: Annotated[StrictInt, Field(ge=1, le=MAX_ERROR_PAYLOAD_BYTES)]


class OutcomeReceipt(BoundaryModel):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    outcome: ReportOutcome
    error_class: ErrorClass | None
    error_code: Code | None
    lost_reason: LostReason | None
    reporting_dispatcher_id: Identifier
    reporting_lease_epoch: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
    replay_digest: Sha256
    response: AcceptedResponse
    error_payload: ErrorPayloadDescriptor | None

    @model_validator(mode="after")
    def fields_match_outcome(self) -> "OutcomeReceipt":
        failed = self.outcome == "failed"
        if any(
            failed != present
            for present in (
                self.error_class is not None,
                self.error_code is not None,
                self.error_payload is not None,
            )
        ) or (self.outcome == "lost") != (self.lost_reason is not None):
            refuse()
        return self


class ErrorPayload(BoundaryModel):
    format: Literal["attempt-error/1"]
    value: AttemptError


class ReportScope(BoundaryModel):
    namespace_id: Identifier
    attempt_id: Identifier


@dataclass(frozen=True)
class PreparedOutcomeRecord:
    receipt: OutcomeReceipt
    payload: str | None
    payload_state: PayloadState


@dataclass(frozen=True)
class UnrecordedOutcome:
    pass


@dataclass(frozen=True)
class AvailableOutcomeError:
    value: AttemptError
    state: Literal["available", "expiring"]


@dataclass(frozen=True)
class UnavailableOutcomeError:
    state: UnavailableState


@dataclass(frozen=True)
class NoOutcomeError:
    pass


OutcomeContent: TypeAlias = (
    AvailableOutcomeError | UnavailableOutcomeError | NoOutcomeError
)


@dataclass(frozen=True)
class RecordedOutcome:
    receipt: OutcomeReceipt
    content: OutcomeContent


DecodedOutcome: TypeAlias = UnrecordedOutcome | RecordedOutcome


def encoded_document(value: object, max_bytes: int, max_depth: int) -> bytes:
    return canonical_bytes(
        parse_json_object(value, max_depth=max_depth, max_bytes=max_bytes)
    )


def detached_error(error: AttemptError | None) -> AttemptError | None:
    if error is None:
        return None
    details = (
        None
        if error.details is None
        else JsonObjectField(
            parse_json_object(error.details, max_bytes=MAX_ERROR_BYTES)
        )
    )
    return AttemptError(code=error.code, message=error.message, details=details)


def prepare_outcome_record(
    namespace_id: str,
    attempt_id: str,
    grant: GrantBinding,
    report: TerminalReport,
    desired_state: DesiredState,
) -> PreparedOutcomeRecord:
    try:
        scope = ReportScope(namespace_id=namespace_id, attempt_id=attempt_id)
        error = detached_error(report.error)
        checked = TerminalReport(
            outcome=report.outcome,
            error_class=report.error_class,
            error=error,
            lost_reason=report.lost_reason,
            evidence=report.evidence,
        )
        document: object = {
            "format": REPLAY_FORMAT,
            "namespace_id": scope.namespace_id,
            "attempt_id": scope.attempt_id,
            "grant": grant.model_dump(mode="json"),
            "report": checked.model_dump(mode="json"),
        }
        replay = encoded_document(document, MAX_REPORT_BYTES, MAX_REPORT_DEPTH)
        descriptor: ErrorPayloadDescriptor | None = None
        payload: str | None = None
        state: PayloadState = "not_applicable"
        if error is not None:
            envelope = ErrorPayload(format="attempt-error/1", value=error)
            raw = encoded_document(
                envelope.model_dump(mode="json"),
                MAX_ERROR_PAYLOAD_BYTES,
                DEFAULT_MAX_DEPTH + 3,
            )
            descriptor = ErrorPayloadDescriptor(
                format="attempt-error/1",
                sha256=hashlib.sha256(raw).hexdigest(),
                size=len(raw),
            )
            payload = raw.decode("utf-8")
            state = "available"
        receipt = OutcomeReceipt(
            version=1,
            outcome=checked.outcome,
            error_class=checked.error_class,
            error_code=None if error is None else error.code,
            lost_reason=checked.lost_reason,
            reporting_dispatcher_id=grant.dispatcher_id,
            reporting_lease_epoch=grant.lease_epoch,
            replay_digest=hashlib.sha256(replay).hexdigest(),
            response=AcceptedResponse(status="finishing", desired_state=desired_state),
            error_payload=descriptor,
        )
        return PreparedOutcomeRecord(receipt, payload, state)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise OutcomeRecordRejected() from None


def unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            refuse()
        result[key] = value
    return result


def invalid_constant(value: str) -> Never:
    refuse()


def decoded_error(
    payload: str, descriptor: ErrorPayloadDescriptor, code: str | None
) -> AttemptError:
    if (
        len(payload) > MAX_ERROR_PAYLOAD_BYTES
        or not fits_utf8(payload, MAX_ERROR_PAYLOAD_BYTES)
        or "\u0000" in payload
    ):
        refuse()
    raw = payload.encode("utf-8")
    if (
        len(raw) != descriptor.size
        or hashlib.sha256(raw).hexdigest() != descriptor.sha256
    ):
        refuse()
    value: object = json.loads(
        payload, object_pairs_hook=unique_members, parse_constant=invalid_constant
    )
    canonical = encoded_document(value, MAX_ERROR_PAYLOAD_BYTES, DEFAULT_MAX_DEPTH + 3)
    if canonical != raw:
        refuse()
    envelope = ErrorPayload.model_validate(value)
    if envelope.value.code != code:
        refuse()
    return envelope.value


def decode_outcome_record(
    receipt_value: JsonValue | None, payload: str | None, state: str | None
) -> DecodedOutcome:
    if receipt_value is None:
        if payload is not None or state is not None:
            refuse()
        return UnrecordedOutcome()
    try:
        receipt_document = parse_json_object(
            receipt_value, max_depth=4, max_bytes=MAX_RECEIPT_BYTES
        )
        receipt = OutcomeReceipt.model_validate(receipt_document)
        if receipt.error_payload is None:
            if payload is not None or state != "not_applicable":
                refuse()
            content: OutcomeContent = NoOutcomeError()
        elif state in ("available", "expiring"):
            if payload is None:
                refuse()
            error = decoded_error(payload, receipt.error_payload, receipt.error_code)
            content = AvailableOutcomeError(error, state)
        elif state in ("expired", "missing"):
            if payload is not None:
                refuse()
            content = UnavailableOutcomeError(state)
        else:
            refuse()
        return RecordedOutcome(receipt, content)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise OutcomeRecordRejected() from None


def receipt_document(receipt: OutcomeReceipt) -> dict[str, JsonValue]:
    try:
        return parse_json_object(
            receipt.model_dump(mode="json"), max_depth=4, max_bytes=MAX_RECEIPT_BYTES
        )
    except (ValidationError, JsonShapeError):
        raise OutcomeRecordRejected() from None
