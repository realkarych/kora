import hashlib
import json

import pytest
from pydantic import ValidationError

from kora.db.types import JsonValue
from kora.engine.outcome_records import (
    MAX_ERROR_PAYLOAD_BYTES,
    AvailableOutcomeError,
    NoOutcomeError,
    OutcomeReceipt,
    OutcomeRecordRejected,
    PreparedOutcomeRecord,
    RecordedOutcome,
    UnavailableOutcomeError,
    UnrecordedOutcome,
    decode_outcome_record,
    prepare_outcome_record,
    receipt_document,
)
from kora.protocols.identity import GrantBinding, encoded_size
from kora.protocols.report import MAX_ERROR_BYTES, AttemptError, TerminalReport
from tests.protocols import runtime_documents as documents


def failed_report(details: dict[str, JsonValue]) -> TerminalReport:
    return TerminalReport.model_validate(
        documents.terminal_report(
            outcome="failed",
            error_class="command_failed",
            error=documents.attempt_error(message=None, details=details),
        )
    )


def prepared(details: dict[str, JsonValue]) -> PreparedOutcomeRecord:
    return prepare_outcome_record(
        documents.NAMESPACE,
        documents.ATTEMPT,
        GrantBinding.model_validate(documents.grant(1)),
        failed_report(details),
        "run",
    )


@pytest.mark.parametrize("character", ["é", "🙂", "\u0000", "\\", '"'])
def test_outcome_record_preserves_admitted_error(character: str) -> None:
    empty = AttemptError.model_validate(
        documents.attempt_error(message=None, details={"text": "", "\u0000": "\\u0000"})
    )
    remaining = MAX_ERROR_BYTES - encoded_size(empty)
    cost = len(json.dumps(character, ensure_ascii=False).encode()) - 2
    text = character * (remaining // cost) + "a" * (remaining % cost)
    report = failed_report({"text": text, "\u0000": "\\u0000"})
    grant = GrantBinding.model_validate(documents.grant(1))

    record = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, grant, report, "run"
    )
    result = decode_outcome_record(
        receipt_document(record.receipt), record.payload, record.payload_state
    )

    assert isinstance(result, RecordedOutcome)
    assert isinstance(result.content, AvailableOutcomeError)
    assert result.content.value == report.error
    assert report.error is not None
    assert encoded_size(report.error) == MAX_ERROR_BYTES
    assert record.payload is not None
    assert "\u0000" not in record.payload
    assert len(record.payload.encode()) <= MAX_ERROR_PAYLOAD_BYTES
    assert record.receipt.response.status == "finishing"


def test_outcome_record_preserves_maximum_details_depth_and_detaches_input() -> None:
    nested: JsonValue = "original"
    for _ in range(15):
        nested = {"next": nested}
    report = failed_report({"value": nested})
    assert report.error is not None
    assert report.error.details is not None

    record = prepare_outcome_record(
        documents.NAMESPACE,
        documents.ATTEMPT,
        GrantBinding.model_validate(documents.grant(1)),
        report,
        "cancel",
    )
    report.error.details["value"] = "changed"
    decoded = decode_outcome_record(
        receipt_document(record.receipt), record.payload, record.payload_state
    )

    assert isinstance(decoded, RecordedOutcome)
    assert isinstance(decoded.content, AvailableOutcomeError)
    assert decoded.content.value.details == {"value": nested}
    assert record.receipt.response.desired_state == "cancel"


@pytest.mark.parametrize(
    ("first", "second"),
    [(1, 1.0), (-0.0, 0.0), ([1, 2], [2, 1]), ("é", "e\u0301"), ("\u0000", "\\u0000")],
)
def test_outcome_replay_distinguishes_typed_original_values(
    first: JsonValue, second: JsonValue
) -> None:
    one = prepared({"value": first})

    two = prepared({"value": second})

    assert one.receipt.replay_digest != two.receipt.replay_digest


def test_outcome_replay_identity_binds_original_grant_and_evidence() -> None:
    grant = GrantBinding.model_validate(documents.grant(1))
    report = failed_report({"one": 1, "two": 2})
    changed_grant = GrantBinding.model_validate(documents.grant(2))
    changed_report = TerminalReport.model_validate(
        documents.terminal_report(
            outcome="failed",
            error_class="command_failed",
            error=documents.attempt_error(message=None, details={"two": 2, "one": 1}),
            evidence=documents.closure_evidence(
                runtime=documents.normal_completion(record_ref="other-record")
            ),
        )
    )

    first = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, grant, report, "run"
    )
    same = prepared({"two": 2, "one": 1})
    other_grant = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, changed_grant, report, "run"
    )
    other_evidence = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, grant, changed_report, "run"
    )

    assert same.receipt.replay_digest == first.receipt.replay_digest
    assert other_grant.receipt.replay_digest != first.receipt.replay_digest
    assert other_evidence.receipt.replay_digest != first.receipt.replay_digest


@pytest.mark.parametrize("state", ["expired", "missing"])
def test_outcome_receipt_survives_payload_expiry(state: str) -> None:
    record = prepared({"private": "retained only as identity"})

    result = decode_outcome_record(receipt_document(record.receipt), None, state)

    assert isinstance(result, RecordedOutcome)
    assert isinstance(result.content, UnavailableOutcomeError)
    assert result.content.state == state
    assert result.receipt == record.receipt


@pytest.mark.parametrize(
    "body",
    [
        '{"format":"attempt-error/1","format":"attempt-error/1","value":{}}',
        '{"format":"attempt-error/2","value":{}}',
        '{"format":"attempt-error/1","value":{"code":"private_marker","message":null,"details":{"v":NaN}}}',
        '{"format":"attempt-error/1","value":{"code":"private_marker","message":null,"details":{"v":"\\ud800"}}}',
    ],
)
def test_outcome_decode_refuses_corrupt_content_without_disclosure(body: str) -> None:
    record = prepared({})
    value = receipt_document(record.receipt)
    value["error_payload"] = {
        "format": "attempt-error/1",
        "size": len(body.encode()),
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }

    with pytest.raises(
        OutcomeRecordRejected, match="^the original outcome record is invalid$"
    ):
        decode_outcome_record(value, body, "available")


def test_outcome_decode_preserves_unrecorded_and_nonfailed_states() -> None:
    report = TerminalReport.model_validate(documents.terminal_report())
    record = prepare_outcome_record(
        documents.NAMESPACE,
        documents.ATTEMPT,
        GrantBinding.model_validate(documents.grant(1)),
        report,
        "run",
    )

    unrecorded = decode_outcome_record(None, None, None)
    recorded = decode_outcome_record(
        receipt_document(record.receipt), None, "not_applicable"
    )

    assert isinstance(unrecorded, UnrecordedOutcome)
    assert isinstance(recorded, RecordedOutcome)
    assert isinstance(recorded.content, NoOutcomeError)


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
def test_outcome_decode_refuses_unknown_or_coerced_versions(version: JsonValue) -> None:
    record = prepared({})
    value = {**receipt_document(record.receipt), "version": version}

    with pytest.raises(OutcomeRecordRejected):
        decode_outcome_record(value, record.payload, "available")


def test_outcome_receipt_has_closed_required_shape() -> None:
    record = prepared({})
    invalid = {**receipt_document(record.receipt), "unexpected": "private_marker"}

    with pytest.raises(ValidationError):
        OutcomeReceipt.model_validate(invalid)


def test_outcome_replay_preserves_full_process_record_identity() -> None:
    identity = {
        "host_id": "host",
        "boot_id": "boot",
        "pid_namespace": "namespace",
        "pid": 100,
        "start_time": 1,
    }
    first = GrantBinding.model_validate(
        documents.grant(1, process={**identity, "leads_group": True})
    )
    second = GrantBinding.model_validate(
        documents.grant(1, process={**identity, "leads_group": False})
    )
    report = failed_report({})

    one = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, first, report, "run"
    )
    two = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, second, report, "run"
    )

    assert first.process == second.process
    assert one.receipt.replay_digest != two.receipt.replay_digest


def test_outcome_replay_admits_complete_report_larger_than_a_single_component() -> None:
    reference = {**documents.VCS_REF, "diff_stat": ""}
    base_size = len(
        json.dumps(
            reference, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    reference["diff_stat"] = "d" * (1_048_576 - base_size)
    report = TerminalReport.model_validate(
        documents.terminal_report(
            evidence=documents.closure_evidence(
                publication=documents.outcome(
                    state="completed", operation_id="operation-1", ref=reference
                )
            )
        )
    )
    grant = GrantBinding.model_validate(documents.grant(1))

    record = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, grant, report, "run"
    )

    assert (
        len(json.dumps(report.model_dump(mode="json"), ensure_ascii=False).encode())
        > 1_048_576
    )
    assert len(record.receipt.replay_digest) == 64
    assert record.payload is None


@pytest.mark.parametrize("outcome", ["cancelled", "lost"])
def test_original_nonresult_outcome_keeps_response_and_reason(outcome: str) -> None:
    report = TerminalReport.model_validate(
        documents.terminal_report(
            outcome=outcome,
            lost_reason="execution_gone" if outcome == "lost" else None,
            evidence=documents.closure_evidence(runtime=documents.termination()),
        )
    )
    grant = GrantBinding.model_validate(documents.grant(1))

    record = prepare_outcome_record(
        documents.NAMESPACE, documents.ATTEMPT, grant, report, "run"
    )
    result = decode_outcome_record(
        receipt_document(record.receipt), record.payload, record.payload_state
    )

    assert isinstance(result, RecordedOutcome)
    assert isinstance(result.content, NoOutcomeError)
    assert result.receipt.outcome == outcome
    assert result.receipt.lost_reason == report.lost_reason
    assert result.receipt.response.desired_state == "run"
