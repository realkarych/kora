import base64
import json

import pytest
from pydantic import ValidationError

from kora.ir.vcs_ref_json import canonical_bytes
from kora.protocols.common import BoundaryModel, Pending
from kora.protocols.evidence import (
    ClosureEvidence,
    DispatcherClosure,
    DispatcherQuiescence,
    EvidenceScope,
    NormalCompletionQuiescence,
    PublicationOutcome,
    TerminationReceipt,
)
from kora.protocols.harness import SessionFile, SessionResult, SessionSpec
from kora.protocols.history import (
    AcknowledgeRead,
    BeginRead,
    ClosurePosition,
    ClosureSnapshot,
    EvidenceDisposition,
    GrantHistoryPage,
    GrantHistoryPosition,
    GrantHistoryProgress,
    GrantHistorySnapshot,
    RecoveryEvidence,
    RecoveryPage,
    RecoveryProgress,
    ResumeRead,
)
from kora.protocols.identity import (
    MAX_COUNT,
    MAX_PID,
    GrantBinding,
    Handle,
    ProcessIdentity,
    encoded_size,
)
from kora.protocols.report import AttemptError, TerminalReport
from kora.protocols.runtime import (
    AdoptedSession,
    ArtifactRef,
    Liveness,
    PublicationAuthorization,
    RawEvent,
    ResourceRef,
    RuntimeStatus,
    TransferItem,
    Workload,
)
from kora.protocols.vcs import (
    MAX_BINDING_BYTES,
    MAX_BINDING_REVISION,
    CheckoutPlan,
    CredentialRequest,
    CredentialRights,
    InspectionContent,
    InspectionManifest,
    IssuedCredential,
    PlanFile,
    PlanStep,
    PreparedPublication,
    PublicationInput,
    PublishPlan,
    WorkspaceRef,
    WorkspaceTarget,
)
from tests.protocols import runtime_documents as documents
from tests.protocols.harness_documents import (
    DIGEST,
    SESSION_FILE,
    SESSION_SPEC,
    WORKSPACE_REF,
    WORKSPACE_TARGET,
    session_result,
)

IDENTITY: dict[str, object] = {
    "host_id": "host-1",
    "boot_id": "boot-1",
    "pid_namespace": "pidns-1",
    "pid": 4242,
    "start_time": 98765,
    "leads_group": True,
}
BINDING: dict[str, object] = {
    "dispatcher_id": "dispatcher-1",
    "worker_id": "worker-1",
    "incarnation_id": "incarnation-1",
    "lease_epoch": 3,
    "execution_domain_id": "domain-1",
    "process": IDENTITY,
}
LEAK_MARKER: str = "has space"
REFUSED_IDENTITIES: list[dict[str, object]] = [
    {**IDENTITY, "pid": 0},
    {**IDENTITY, "pid": "4242"},
    {**IDENTITY, "pid": MAX_PID + 1},
    {**IDENTITY, "start_time": -1},
    {**IDENTITY, "start_time": MAX_COUNT + 1},
    {**IDENTITY, "host_id": "h" * 257},
    {**IDENTITY, "leads_group": 1},
]
REFUSED_BINDINGS: list[dict[str, object]] = [
    {**BINDING, "lease_epoch": -1},
    {**BINDING, "lease_epoch": MAX_COUNT + 1},
    {**BINDING, "process": {**IDENTITY, "unknown": 1}},
]
MARKED_IDENTIFIERS: dict[type[BoundaryModel], dict[str, object]] = {
    ProcessIdentity: {**IDENTITY, "host_id": LEAK_MARKER},
    GrantBinding: {**BINDING, "worker_id": LEAK_MARKER},
}


def test_the_boundary_base_is_closed_and_hides_input() -> None:
    config = BoundaryModel.model_config

    assert config.get("frozen") is True
    assert config.get("extra") == "forbid"
    assert config.get("hide_input_in_errors") is True


@pytest.mark.parametrize("document", REFUSED_IDENTITIES)
def test_process_identity_refuses_invalid_values(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProcessIdentity.model_validate(document)


@pytest.mark.parametrize("document", REFUSED_BINDINGS)
def test_grant_binding_refuses_invalid_values(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GrantBinding.model_validate(document)


@pytest.mark.parametrize(
    "model", list(MARKED_IDENTIFIERS), ids=lambda model: model.__name__
)
def test_identity_refusals_carry_no_input(model: type[BoundaryModel]) -> None:
    document = MARKED_IDENTIFIERS[model]

    with pytest.raises(ValidationError) as raised:
        model.model_validate(document)

    assert LEAK_MARKER not in str(raised.value)


@pytest.mark.parametrize(
    ("field", "other"),
    [("host_id", "host-2"), ("boot_id", "boot-2"), ("pid", 4243), ("start_time", 1)],
)
def test_identities_differ_when_a_key_field_differs(field: str, other: object) -> None:
    changed = {**IDENTITY, field: other}

    first = ProcessIdentity.model_validate(IDENTITY)
    second = ProcessIdentity.model_validate(changed)

    assert first != second


def test_identity_equality_ignores_group_leadership() -> None:
    leader = ProcessIdentity.model_validate(IDENTITY)
    member = ProcessIdentity.model_validate({**IDENTITY, "leads_group": False})

    assert leader == member
    assert hash(leader) == hash(member)
    assert leader.identity_key() == member.identity_key()


def closed_documents() -> dict[type[BoundaryModel], dict[str, object]]:
    closures = [documents.closure()]
    grants = [documents.grant(1), documents.grant(2)]
    return {
        ProcessIdentity: IDENTITY,
        GrantBinding: BINDING,
        WorkspaceRef: WORKSPACE_REF,
        WorkspaceTarget: WORKSPACE_TARGET,
        SessionFile: SESSION_FILE,
        SessionSpec: SESSION_SPEC,
        SessionResult: session_result(),
        Handle: documents.handle(),
        DispatcherQuiescence: documents.quiescence(),
        TerminationReceipt: documents.termination(),
        NormalCompletionQuiescence: documents.normal_completion(),
        DispatcherClosure: documents.closure(),
        PublicationOutcome: documents.outcome(),
        ClosureEvidence: documents.closure_evidence(),
        AttemptError: documents.attempt_error(),
        TerminalReport: documents.terminal_report(),
        GrantHistorySnapshot: documents.grant_snapshot(grants),
        GrantHistoryPosition: documents.grant_position(grants),
        GrantHistoryPage: documents.grant_page(grants, 0, 2),
        GrantHistoryProgress: {
            "snapshot": documents.grant_snapshot(grants),
            "accepted": documents.grant_position(grants[:1]),
            "complete": False,
        },
        ClosureSnapshot: documents.closure_snapshot(closures),
        ClosurePosition: documents.closure_position(closures),
        EvidenceDisposition: documents.members(
            documents.acknowledge(closures), "dispositions"
        )[0],
        AcknowledgeRead: documents.acknowledge(closures),
        BeginRead: {
            "kind": "begin",
            "snapshot": documents.grant_snapshot(grants),
            "expected_previous_generation": 0,
        },
        ResumeRead: {"kind": "resume"},
        RecoveryProgress: documents.recovery_progress(),
        RecoveryEvidence: documents.recovery_evidence(closures),
        RecoveryPage: documents.recovery_page(closures, 0, 1),
        PlanFile: documents.plan_file(),
        PlanStep: documents.plan_step(),
        CheckoutPlan: documents.checkout_plan(),
        PublishPlan: documents.publish_plan(),
        PublicationInput: {
            "attempt_id": documents.ATTEMPT,
            "format": "git-bundle",
            "location": "sealed/input-1",
            "digest": DIGEST,
        },
        InspectionContent: {
            "revision": "c0ffee",
            "path": "src/app.py",
            "kind": "file",
            "content_location": "inspection/blob-1",
            "content_digest": DIGEST,
            "size_bytes": 10,
        },
        InspectionManifest: {
            "prepared_digest": DIGEST,
            "revision": "c0ffee",
            "content_index_location": "inspection/index",
            "content_index_digest": DIGEST,
            "complete": False,
        },
        PreparedPublication: documents.prepared_publication(),
        CredentialRequest: documents.credential_request(),
        CredentialRights: documents.rights(),
        IssuedCredential: documents.issued_credential(),
        TransferItem: {
            "name": "check.sh",
            "size": 10,
            "sha256": DIGEST,
            "kind": "resource",
        },
        ArtifactRef: documents.member(
            documents.member(documents.workload(), "artifact_inputs"), "plan"
        ),
        ResourceRef: documents.resource_ref(),
        Workload: documents.workload(),
        RuntimeStatus: {
            "state": "exited",
            "exit_code": 0,
            "phase": "complete",
            "observed_at": documents.AT,
        },
        AdoptedSession: {
            "handle": documents.handle(),
            "phase": "awaiting_authorization",
            "prepared": documents.prepared_publication(),
            "journal_next": 7,
            "observational": False,
        },
        RawEvent: {
            "i": 0,
            "at": documents.AT,
            "kind": "runner.journal_gap",
            "data": {"omitted": 3},
        },
        Liveness: documents.liveness(),
        PublicationAuthorization: documents.authorization(),
    }


DOCUMENTS: dict[type[BoundaryModel], dict[str, object]] = closed_documents()


@pytest.mark.parametrize("model", list(DOCUMENTS), ids=lambda model: model.__name__)
def test_protocol_models_are_closed(model: type[BoundaryModel]) -> None:
    document = DOCUMENTS[model]
    unknown = {**document, "unknown": 1}

    parsed = model.model_validate_json(json.dumps(document))
    from_json = model.model_validate_json(parsed.model_dump_json())
    from_python = model.model_validate(parsed.model_dump())

    assert from_json == parsed
    assert from_python == parsed
    assert parsed.model_dump(mode="json") == document
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(unknown))
    with pytest.raises(ValidationError):
        model.model_validate({**parsed.model_dump(), "unknown": 1})


def test_plan_bytes_round_trip_as_base64() -> None:
    raw = bytes(range(256))

    step = PlanStep.model_validate_json(
        json.dumps(documents.plan_step(stdin=base64.urlsafe_b64encode(raw).decode()))
    )

    assert step.stdin == raw
    assert PlanStep.model_validate_json(step.model_dump_json()).stdin == raw


BINDING_OWNERS: list[tuple[type[BoundaryModel], dict[str, object]]] = [
    (CredentialRequest, documents.credential_request()),
    (IssuedCredential, documents.issued_credential()),
    (PublicationAuthorization, documents.authorization()),
]
REFUSED_TUPLES: list[dict[str, object]] = [
    {"binding": ""},
    {"binding": "b" * 257},
    {"binding": "é" * 129},
    {"binding": "line\nbreak"},
    {"binding": "\ud800"},
    {"binding_revision": 0},
    {"binding_revision": 2**53},
    {"binding_revision": True},
    {"binding_revision": 1.0},
    {"binding_revision": "1"},
    {"credential_mode": "credential_optional"},
]
BINDING_FIELDS: tuple[str, ...] = ("binding", "binding_revision", "credential_mode")
ALIAS_REFUSALS: list[dict[str, object]] = [
    {"binding": "é" * (MAX_BINDING_BYTES // 2 + 1)},
    {"binding": "line\nbreak"},
    {"binding_revision": True},
    {"binding_revision": MAX_BINDING_REVISION + 1},
    {"credential_mode": "credential_optional"},
]
BindingTupleCase = tuple[type[BoundaryModel], list[dict[str, object]]]


def without(document: dict[str, object], name: str) -> dict[str, object]:
    return {key: value for key, value in document.items() if key != name}


def binding_tuple_cases() -> dict[str, BindingTupleCase]:
    bounded_model, bounded_document = BINDING_OWNERS[0]
    bounded = {
        f"bounded-{next(iter(change))}-{index}": (
            bounded_model,
            [{**bounded_document, **change}],
        )
        for index, change in enumerate(REFUSED_TUPLES)
    }
    wired = {
        f"wired-{model.__name__}": (
            model,
            [
                *({**document, **change} for change in ALIAS_REFUSALS),
                *(without(document, name) for name in BINDING_FIELDS),
            ],
        )
        for model, document in BINDING_OWNERS
    }
    return {**bounded, **wired}


BINDING_TUPLE_CASES: dict[str, BindingTupleCase] = binding_tuple_cases()


@pytest.mark.parametrize("case", list(BINDING_TUPLE_CASES))
def test_binding_tuple_is_required_and_bounded(case: str) -> None:
    model, refused = BINDING_TUPLE_CASES[case]

    for candidate in refused:
        with pytest.raises(ValidationError):
            model.model_validate(candidate)


CONTRADICTORY_CREDENTIALS: list[dict[str, object]] = [
    {"rights": documents.rights()},
    {"expires_at": documents.AT},
    {"replacement": "static"},
    {"revocation": "manual"},
    {"principal": documents.APP_PRINCIPAL},
    {"credential_mode": "credential_required"},
    {
        "credential_mode": "credential_required",
        "principal": documents.APP_PRINCIPAL,
        "rights": documents.rights(),
        "replacement": "reissue",
        "revocation": "native",
    },
]
CONTRADICTORY_AUTHORIZATIONS: list[dict[str, object]] = [
    {"principal": documents.APP_PRINCIPAL},
    {"credential_mode": "credential_required"},
    {"credential_mode": "credential_required", "principal": documents.APP_PRINCIPAL},
]


@pytest.mark.parametrize("change", CONTRADICTORY_CREDENTIALS)
def test_issued_credential_refuses_contradictions(change: dict[str, object]) -> None:
    document = {**documents.issued_credential(), **change}

    with pytest.raises(ValidationError):
        IssuedCredential.model_validate(document)


@pytest.mark.parametrize("change", CONTRADICTORY_AUTHORIZATIONS)
def test_authorization_refuses_contradictions(change: dict[str, object]) -> None:
    document = {**documents.authorization(), **change}

    with pytest.raises(ValidationError):
        PublicationAuthorization.model_validate(document)


def test_credential_value_has_no_default() -> None:
    issued = {
        key: value
        for key, value in documents.issued_credential().items()
        if key != "value"
    }
    authorization = {
        key: value
        for key, value in documents.authorization().items()
        if key != "credential"
    }

    with pytest.raises(ValidationError):
        IssuedCredential.model_validate(issued)
    with pytest.raises(ValidationError):
        PublicationAuthorization.model_validate(authorization)


def pending_marks() -> dict[str, int]:
    marks: dict[str, int] = {}
    for model in DOCUMENTS:
        for name, field in model.model_fields.items():
            for item in field.metadata:
                if isinstance(item, Pending):
                    marks[f"{model.__name__}.{name}"] = item.issue
    return marks


def test_pending_marks_are_enumerated() -> None:
    marks = pending_marks()

    assert marks == {"Workload.artifact_inputs": 106}


COUNTED_MODULES: frozenset[str] = frozenset(
    {
        "kora.protocols.identity",
        "kora.protocols.identity_records",
        "kora.protocols.evidence",
        "kora.protocols.report",
        "kora.protocols.history",
    }
)


@pytest.mark.parametrize(
    "model",
    [
        model
        for model, document in DOCUMENTS.items()
        if model.__module__ in COUNTED_MODULES
        and "pushed_by" not in json.dumps(document)
    ],
    ids=lambda model: model.__name__,
)
def test_encoded_size_matches_canonical_bytes(model: type[BoundaryModel]) -> None:
    parsed = model.model_validate_json(json.dumps(DOCUMENTS[model]))

    expected = len(canonical_bytes(parsed.model_dump(mode="json")))

    assert encoded_size(parsed) == expected


def test_evidence_scopes_are_the_closed_local_set() -> None:
    scopes = {scope.value for scope in EvidenceScope}

    assert scopes == {
        "subreaper+leader_group+env_sweep",
        "leader_group+env_sweep",
        "env_sweep",
        "no_runner_started",
        "fake_runtime",
    }
    with pytest.raises(ValidationError):
        TerminationReceipt.model_validate(documents.termination(scope="cgroup"))
