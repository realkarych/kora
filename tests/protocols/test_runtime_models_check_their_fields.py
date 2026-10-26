import json

import pytest
from pydantic import BaseModel, ValidationError

from kora.ir.vcs_ref_json import canonical_bytes
from kora.protocols.evidence import (
    MAX_DISPATCHERS,
    ClosureEvidence,
    DispatcherClosure,
    PublicationOutcome,
    dispatchers_envelope_size,
)
from kora.protocols.history import RecoveryEvidence
from kora.protocols.report import MAX_ERROR_BYTES, AttemptError, TerminalReport
from kora.protocols.runtime import (
    Liveness,
    RawEvent,
    ResourceRef,
    RuntimeStatus,
    TransferItem,
    Workload,
)
from tests.protocols import runtime_documents as documents
from tests.protocols.harness_documents import DIGEST, WORKSPACE_REF

PROCESS: dict[str, object] = {
    "host_id": "host-1",
    "boot_id": None,
    "pid_namespace": None,
    "pid": 42,
    "start_time": 7,
    "leads_group": True,
}
FAILED_ERROR: dict[str, object] = {
    "error_class": "command_failed",
    "error": documents.attempt_error(),
}
RECEIPT_EVIDENCE: dict[str, object] = documents.closure_evidence(
    runtime=documents.termination()
)
REFUSED: list[tuple[type[BaseModel], dict[str, object]]] = [
    (TerminalReport, documents.terminal_report(outcome="failed")),
    (TerminalReport, documents.terminal_report(**FAILED_ERROR)),
    (TerminalReport, documents.terminal_report(lost_reason="execution_gone")),
    (TerminalReport, documents.terminal_report(outcome="lost")),
    (
        TerminalReport,
        documents.terminal_report(outcome="cancelled"),
    ),
    (
        TerminalReport,
        documents.terminal_report(
            outcome="lost",
            lost_reason="execution_gone",
            evidence=documents.closure_evidence(),
        ),
    ),
    (
        TerminalReport,
        documents.terminal_report(
            evidence=documents.closure_evidence(publication=None)
        ),
    ),
    (
        TerminalReport,
        documents.terminal_report(evidence=documents.closure_evidence(runtime=None)),
    ),
    (
        ClosureEvidence,
        documents.closure_evidence(runtime={**documents.termination(), "kind": None}),
    ),
    (
        ClosureEvidence,
        documents.closure_evidence(
            runtime={**documents.termination(), "kind": "normal_completion"}
        ),
    ),
    (
        ClosureEvidence,
        documents.closure_evidence(dispatchers=[documents.closure()] * 2),
    ),
    (
        ClosureEvidence,
        documents.closure_evidence(
            runtime=documents.normal_completion(attempt_id="attempt-2")
        ),
    ),
    (
        ClosureEvidence,
        documents.closure_evidence(
            dispatchers=documents.dispatcher_closures(MAX_DISPATCHERS + 1)
        ),
    ),
    (DispatcherClosure, documents.closure(process=PROCESS)),
    (DispatcherClosure, documents.closure(provenance="runtime_observed_process_exit")),
    (PublicationOutcome, documents.outcome(state="completed", operation_id="op-1")),
    (PublicationOutcome, documents.outcome(state="failed", operation_id="op-1")),
    (
        PublicationOutcome,
        documents.outcome(state="failed", code="push_rejected"),
    ),
    (PublicationOutcome, documents.outcome(operation_id="op-1")),
    (PublicationOutcome, documents.outcome(code="push_rejected")),
    (PublicationOutcome, documents.outcome(state="failed", code="Bad Code")),
    (AttemptError, documents.attempt_error(code="Bad Code")),
    (AttemptError, documents.attempt_error(message="line\nbreak")),
    (AttemptError, documents.attempt_error(message="m" * 4097)),
    (AttemptError, documents.attempt_error(details={"text": "x" * MAX_ERROR_BYTES})),
    (AttemptError, documents.attempt_error(details={"value": float("nan")})),
    (RuntimeStatus, {"state": "running"}),
    (
        Liveness,
        documents.liveness(pricing_status="unpriced"),
    ),
    (Liveness, documents.liveness(input_tokens=-1)),
    (Liveness, documents.liveness(observed_at="2026-10-25T12:00:00")),
    (RawEvent, {"i": 0, "at": documents.AT, "kind": "has space", "data": {}}),
    (
        RawEvent,
        {"i": 0, "at": documents.AT, "kind": "output", "data": {"t": "x" * 65_536}},
    ),
    (
        RecoveryEvidence,
        documents.recovery_evidence([documents.closure()], retired=False),
    ),
    (
        RecoveryEvidence,
        documents.recovery_evidence(
            [documents.closure()], handle=documents.handle(attempt_id="attempt-2")
        ),
    ),
    (
        RecoveryEvidence,
        documents.recovery_evidence(
            [documents.closure()],
            normal_completion=documents.normal_completion(
                execution_domain_id="domain-2"
            ),
        ),
    ),
]
TRANSFER_NAMES: list[object] = [".", "..", "a/b", "nul\x00", "tab\t", "é" * 128, ""]
RESOURCE_NAMES: list[object] = [
    "/abs",
    "a/../b",
    "a" * 256,
    "/".join(["a" * 255] * 17),
]


def status(**changes: object) -> dict[str, object]:
    return {
        "state": "exited",
        "exit_code": 0,
        "phase": "complete",
        "observed_at": documents.AT,
        **changes,
    }


@pytest.mark.parametrize(("model", "document"), REFUSED)
def test_runtime_models_refuse_contradictions(
    model: type[BaseModel], document: dict[str, object]
) -> None:
    candidate = status(**document) if model is RuntimeStatus else document

    with pytest.raises(ValidationError) as raised:
        model.model_validate_json(json.dumps(candidate, allow_nan=True))

    assert "has space" not in str(raised.value)


def test_runtime_models_accept_every_outcome_shape() -> None:
    reports = [
        documents.terminal_report(outcome="failed", **FAILED_ERROR),
        documents.terminal_report(outcome="cancelled", evidence=RECEIPT_EVIDENCE),
        documents.terminal_report(
            outcome="lost", lost_reason="checkpoint_missing", evidence=RECEIPT_EVIDENCE
        ),
    ]
    outcomes = [
        documents.outcome(
            state="completed", operation_id="op-1", ref=documents.VCS_REF
        ),
        documents.outcome(state="failed", operation_id="op-1", code="push_rejected"),
        documents.outcome(state="unknown"),
        documents.outcome(state="not_dispatched", operation_id="op-1"),
    ]

    parsed_reports = [TerminalReport.model_validate(report) for report in reports]
    parsed_outcomes = [PublicationOutcome.model_validate(item) for item in outcomes]
    observed = DispatcherClosure.model_validate(
        documents.closure(provenance="runtime_observed_process_exit", process=PROCESS)
    )

    assert [report.outcome for report in parsed_reports] == [
        "failed",
        "cancelled",
        "lost",
    ]
    assert [item.state for item in parsed_outcomes] == [
        "completed",
        "failed",
        "unknown",
        "not_dispatched",
    ]
    assert observed.process is not None


def test_dispatcher_envelope_counter_matches_canonical_bytes() -> None:
    closures = documents.dispatcher_closures(3) + [
        documents.closure(
            quiescence=documents.quiescence(dispatcher_id='quote"slash\\'),
            provenance="runtime_observed_process_exit",
            process=PROCESS,
        )
    ]

    evidence = ClosureEvidence.model_validate(
        documents.closure_evidence(dispatchers=closures)
    )
    encoded = canonical_bytes(
        {"dispatchers": evidence.model_dump(mode="json")["dispatchers"]}
    )

    assert dispatchers_envelope_size(evidence.dispatchers) == len(encoded)


def test_attempt_error_is_bounded_as_a_whole() -> None:
    message = "m" * 4096
    room = MAX_ERROR_BYTES - len(
        canonical_bytes(
            {"code": "command_failed", "message": message, "details": {"t": ""}}
        )
    )

    fitting = documents.attempt_error(message=message, details={"t": "x" * room})
    over = documents.attempt_error(message=message, details={"t": "x" * (room + 1)})

    assert AttemptError.model_validate(fitting).details == {"t": "x" * room}
    with pytest.raises(ValidationError):
        AttemptError.model_validate(over)


@pytest.mark.parametrize("name", TRANSFER_NAMES)
def test_transfer_item_names_are_one_component(name: object) -> None:
    document = {"name": name, "size": 1, "sha256": DIGEST, "kind": "input"}

    with pytest.raises(ValidationError):
        TransferItem.model_validate(document)


def test_transfer_item_size_is_bounded() -> None:
    document = {"name": "a", "size": 2**30, "sha256": DIGEST, "kind": "script"}

    accepted = TransferItem.model_validate(document)

    assert accepted.size == 2**30
    with pytest.raises(ValidationError):
        TransferItem.model_validate({**document, "size": 2**30 + 1})


@pytest.mark.parametrize("name", RESOURCE_NAMES)
def test_resource_names_are_bounded(name: object) -> None:
    document = documents.resource_ref(name=name)

    with pytest.raises(ValidationError):
        ResourceRef.model_validate(document)


@pytest.mark.parametrize(
    "change",
    [
        {"size": 262_145},
        {"secret_refs": {"TOKEN": "env/TOKEN"}},
        {"harness_credentials": {"lower": "secret://env/X"}},
        {"env": {f"K{index}": "v" for index in range(257)}},
        {"resources": [documents.resource_ref()] * 65},
        {"checkout": {**documents.checkout_plan(), **WORKSPACE_REF}},
        {"checkout": {"root": "/workspace"}},
        {"artifact_inputs": {"plan": {"artifact_version_id": "v"}}},
    ],
)
def test_workload_refuses_unbounded_or_ambiguous_fields(
    change: dict[str, object],
) -> None:
    document = (
        documents.workload(resources=[documents.resource_ref(**change)])
        if "size" in change
        else documents.workload(**change)
    )

    with pytest.raises(ValidationError):
        Workload.model_validate(document)


def test_workload_accepts_a_workspace_checkout() -> None:
    document = documents.workload(checkout=WORKSPACE_REF, publish=None)

    workload = Workload.model_validate(document)

    assert workload.model_dump(mode="json")["checkout"] == WORKSPACE_REF


@pytest.mark.parametrize(
    ("given", "written"),
    [
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000Z"),
        ("2026-10-25T15:00:00.5+03:00", "2026-10-25T12:00:00.500000Z"),
        ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999999Z"),
    ],
)
def test_instants_are_written_with_six_fractional_digits(
    given: str, written: str
) -> None:
    document = status(observed_at=given)

    parsed = RuntimeStatus.model_validate(document)

    assert parsed.model_dump(mode="json")["observed_at"] == written


@pytest.mark.parametrize(
    "value", ["2026-10-25T12:00:00", "0001-01-01T00:00:00+01:00", 1_700_000_000, "x"]
)
def test_instants_refuse_naive_overflowing_and_numeric_input(value: object) -> None:
    document = status(observed_at=value)

    with pytest.raises(ValidationError):
        RuntimeStatus.model_validate(document)
