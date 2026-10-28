import json
from typing import Annotated

import pytest
from pydantic import Field, TypeAdapter, ValidationError

from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    Acquisition,
    ActivationAcknowledgement,
    BorrowedServiceRegistration,
    BorrowedServiceRequest,
    CleanupOutcome,
    CleanupReference,
    ClosureReport,
    EnvironmentVariable,
    LifetimeLimits,
    LifetimeReason,
    LocalRuntimeBinding,
    LocalRuntimeLaunch,
    LocalRuntimeRegistration,
    OperationId,
    OriginalOutcome,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    Reservation,
    ReservationRequest,
    ReservationToken,
    ResourceKind,
    ResourceObservation,
    ResourceState,
    TemporaryTreeRegistration,
    TemporaryTreeRequest,
)

INVOCATION_ID = "0123456789abcdef0123456789abcdef"
RESOURCE_ID = "1123456789abcdef0123456789abcdef"
ROOT_RESOURCE_ID = "2123456789abcdef0123456789abcdef"
OPERATION_ID = "3123456789abcdef0123456789abcdef"
DIGEST = "sha256:" + "a" * 64
AcquisitionRequest = Annotated[
    ProcessLaunch | LocalRuntimeLaunch | TemporaryTreeRequest | BorrowedServiceRequest,
    Field(discriminator="kind"),
]


def process_identity() -> ProcessIdentity:
    return ProcessIdentity(
        host_id="host",
        boot_id="boot",
        pid_namespace="namespace",
        pid=100,
        start_time=200,
        leads_group=True,
    )


def reservation_token() -> ReservationToken:
    return ReservationToken(
        invocation_id=INVOCATION_ID,
        resource_id=RESOURCE_ID,
        owner=process_identity(),
        generation=1,
    )


def runtime_binding() -> LocalRuntimeBinding:
    return LocalRuntimeBinding(
        execution_domain_id="domain",
        state_root_resource_id=ROOT_RESOURCE_ID,
        attempt_id="attempt",
        launch_nonce="nonce",
        request_digest=DIGEST,
    )


def process_launch() -> ProcessLaunch:
    return ProcessLaunch(
        executable="/bin/tool",
        argv=("tool", "--run"),
        environment=(EnvironmentVariable(name="MODE", value="test"),),
        working_directory_resource_id=ROOT_RESOURCE_ID,
        stdin=None,
        stdout=ProcessOutput.CAPTURE,
        stderr=None,
        domain=ProcessDomain.DETACHED,
    )


def process_registration() -> ProcessRegistration:
    return ProcessRegistration(
        process=process_identity(),
        acquisition_operation_id=OPERATION_ID,
        process_group_id=100,
        session_id=100,
    )


def observation() -> ResourceObservation:
    return ResourceObservation(
        resource_id=RESOURCE_ID,
        kind=ResourceKind.PROCESS_DOMAIN,
        revision=1,
        state=ResourceState.UNKNOWN,
        pending_acquisition_count=1,
        live_owned_process_count=0,
        remaining_pipe_count=0,
        remaining_tree_count=0,
        reason=LifetimeReason.EFFECT_UNSETTLED,
    )


@pytest.mark.parametrize(
    "invalid", ["a" * 31, "a" * 33, "A" * 32, "a" * 32 + "\n", 1, True]
)
def test_operation_identity_rejects_noncanonical_wire_values(invalid: object) -> None:
    adapter = TypeAdapter(OperationId)

    with pytest.raises(ValidationError) as rejected:
        adapter.validate_python(invalid)

    assert rejected.value.error_count() == 1


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        *(("generation", invalid) for invalid in [True, 0, 2**64]),
        *(("revision", invalid) for invalid in [True, "1", 1.0, 0, -1, 2**64]),
    ],
)
def test_authority_counters_require_positive_uint64(
    field: str, invalid: object
) -> None:
    model = ReservationToken if field == "generation" else Reservation
    value: dict[str, object] = (
        reservation_token().model_dump()
        if field == "generation"
        else {
            "token": reservation_token(),
            "kind": "process_domain",
            "revision": 1,
            "state": "reserved",
        }
    )
    value[field] = invalid

    with pytest.raises(ValidationError) as rejected:
        model.model_validate(value)

    assert field in {str(error["loc"][0]) for error in rejected.value.errors()}


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        *(
            ("acquisition_seconds", invalid)
            for invalid in [True, "1", 0, -1, float("inf"), float("nan")]
        ),
        *(
            (field, invalid)
            for field in [
                "cooperative_seconds",
                "term_seconds",
                "kill_observation_seconds",
                "drain_seconds",
            ]
            for invalid in [True, 0, float("inf")]
        ),
    ],
)
def test_lifetime_deadlines_refuse_nonfinite_nonpositive_or_coerced_values(
    field: str,
    invalid: object,
) -> None:
    value = {field: invalid}

    with pytest.raises(ValidationError) as rejected:
        LifetimeLimits.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    ("field", "maximum"),
    [
        ("max_resource_records", 256),
        ("max_outstanding_operations", 16),
        ("max_control_frame_bytes", 65536),
        ("max_control_recovery_bytes", 1048576),
        ("max_execution_snapshot_bytes", 1048576),
        ("max_unresolved_samples", 32),
        ("max_state_root_invocations", 32),
    ],
)
def test_lifetime_capacity_cannot_weaken_absolute_admission_bounds(
    field: str, maximum: int
) -> None:
    value = {field: maximum + 1}

    with pytest.raises(ValidationError) as rejected:
        LifetimeLimits.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    ("kind", "binding", "valid"),
    [
        ("process_domain", None, True),
        ("temporary_tree", None, True),
        ("borrowed_service", None, True),
        ("local_runtime_domain", "binding", True),
        ("process_domain", "binding", False),
        ("local_runtime_domain", None, False),
    ],
)
def test_reservations_bind_runtime_identity_exactly_for_runtime_domains(
    kind: str,
    binding: str | None,
    valid: bool,
) -> None:
    value = {
        "operation_id": OPERATION_ID,
        "kind": kind,
        "runtime_binding": runtime_binding() if binding else None,
    }

    if valid:
        request = ReservationRequest.model_validate(value)

        assert (request.runtime_binding is not None) == (kind == "local_runtime_domain")
    else:
        with pytest.raises(ValidationError) as rejected:
            ReservationRequest.model_validate(value)

        assert "runtime_binding" in str(rejected.value)


@pytest.mark.parametrize(
    "launch_request",
    [
        process_launch(),
        LocalRuntimeLaunch(binding=runtime_binding(), launch=process_launch()),
        TemporaryTreeRequest(parent_resource_id=ROOT_RESOURCE_ID),
        BorrowedServiceRequest(service_reference="service", pre_existing=True),
    ],
)
def test_acquisition_requests_roundtrip_discriminated_closed_values(
    launch_request: AcquisitionRequest,
) -> None:
    adapter = TypeAdapter(AcquisitionRequest)
    payload = adapter.dump_json(launch_request)

    restored = adapter.validate_json(payload)

    assert type(restored) is type(launch_request)
    assert restored == launch_request


@pytest.mark.parametrize(
    "registration",
    [
        process_registration(),
        LocalRuntimeRegistration(
            binding=runtime_binding(), runner=process_registration()
        ),
        TemporaryTreeRegistration(
            resource_id=RESOURCE_ID,
            parent_resource_id=ROOT_RESOURCE_ID,
            name="4" * 32,
            device=1,
            inode=2,
            owner_uid=1000,
        ),
        BorrowedServiceRegistration(
            service_reference="service", connection_scope=ROOT_RESOURCE_ID
        ),
    ],
)
def test_acquisitions_roundtrip_exact_registration_discriminators(
    registration: ProcessRegistration
    | LocalRuntimeRegistration
    | TemporaryTreeRegistration
    | BorrowedServiceRegistration,
) -> None:
    acquisition = Acquisition(
        token=reservation_token(),
        operation_id=OPERATION_ID,
        revision=1,
        registration=registration,
    )

    restored = Acquisition.model_validate_json(acquisition.model_dump_json())

    assert type(restored.registration) is type(registration)
    assert restored == acquisition


def test_acquisition_wire_refuses_untyped_controller_authority() -> None:
    value = {
        "token": reservation_token(),
        "operation_id": OPERATION_ID,
        "revision": 1,
        "registration": process_registration(),
        "handle": "authority",
    }

    with pytest.raises(ValidationError) as rejected:
        Acquisition.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == ("handle",)


def test_registration_discriminator_does_not_fall_back_to_another_shape() -> None:
    value = {
        "token": reservation_token(),
        "operation_id": OPERATION_ID,
        "revision": 1,
        "registration": {
            "kind": "temporary_tree",
            **process_registration().model_dump(exclude={"kind"}),
        },
    }

    with pytest.raises(ValidationError) as rejected:
        Acquisition.model_validate(value)

    assert all(error["loc"][0] == "registration" for error in rejected.value.errors())


@pytest.mark.parametrize("field", ["executable", "argv", "environment"])
def test_launch_refuses_nul_in_every_execution_string(field: str) -> None:
    value = process_launch().model_dump()
    value[field] = {
        "executable": "bad\x00",
        "argv": ("bad\x00",),
        "environment": ({"name": "MODE", "value": "bad\x00"},),
    }[field]

    with pytest.raises(ValidationError) as rejected:
        ProcessLaunch.model_validate(value)

    assert field in {str(error["loc"][0]) for error in rejected.value.errors()}


def test_launch_refuses_duplicate_environment_names() -> None:
    value = process_launch().model_dump()
    value["environment"] = (
        {"name": "MODE", "value": "one"},
        {"name": "MODE", "value": "two"},
    )

    with pytest.raises(ValidationError) as rejected:
        ProcessLaunch.model_validate(value)

    assert "duplicate" in str(rejected.value)


def test_launch_snapshot_counts_utf8_and_working_directory_toward_aggregate_limit() -> (
    None
):
    value = process_launch().model_dump()
    value["argv"] = tuple("é" * 32768 for _ in range(16))

    with pytest.raises(ValidationError) as rejected:
        ProcessLaunch.model_validate(value)

    assert "snapshot" in str(rejected.value)


def test_launch_snapshot_accepts_exact_aggregate_byte_boundary() -> None:
    value = process_launch().model_dump()
    value["argv"] = ("a" * 65536,) * 15 + ("a" * 65487,)

    launch = ProcessLaunch.model_validate(value)

    assert len(launch.argv) == 16


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("stdin", "inherit"),
        ("stdout", "inherit"),
        ("stderr", "control"),
        ("domain", "unknown"),
        ("working_directory_resource_id", "/foreign/tree"),
        ("argv", ["tool"]),
        ("environment", {"MODE": "test"}),
    ],
)
def test_launch_accepts_only_closed_private_execution_inputs(
    field: str, invalid: object
) -> None:
    value = process_launch().model_dump()
    value[field] = invalid

    with pytest.raises(ValidationError) as rejected:
        ProcessLaunch.model_validate(value)

    assert field in {str(error["loc"][0]) for error in rejected.value.errors()}


def test_private_execution_snapshot_is_deeply_immutable() -> None:
    launch = process_launch()

    with pytest.raises(ValidationError) as rejected:
        launch.environment[0].value = "changed"

    assert rejected.value.errors()[0]["type"] == "frozen_instance"
    assert launch.environment[0].value == "test"


@pytest.mark.parametrize("invalid", [False, 1, "true"])
def test_borrowed_services_require_explicit_preexisting_declaration(
    invalid: object,
) -> None:
    value = {"service_reference": "service", "pre_existing": invalid}

    with pytest.raises(ValidationError) as rejected:
        BorrowedServiceRequest.model_validate(value)

    assert "pre_existing" in str(rejected.value)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("device", -1),
        ("inode", 2**64),
        ("owner_uid", 2**32),
        ("owner_uid", True),
        ("name", "not-a-basename"),
        ("name", "a" * 32 + "\n"),
    ],
)
def test_tree_identity_refuses_paths_and_unbounded_numeric_identity(
    field: str, invalid: object
) -> None:
    value: dict[str, object] = {
        "resource_id": RESOURCE_ID,
        "parent_resource_id": ROOT_RESOURCE_ID,
        "name": "4" * 32,
        "device": 1,
        "inode": 2,
        "owner_uid": 1000,
    }
    value[field] = invalid

    with pytest.raises(ValidationError) as rejected:
        TemporaryTreeRegistration.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("invalid", [True, "0", -1, 2**63])
def test_public_observations_refuse_unbounded_or_coerced_counts(
    invalid: object,
) -> None:
    value = observation().model_dump()
    value["pending_acquisition_count"] = invalid

    with pytest.raises(ValidationError) as rejected:
        ResourceObservation.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == ("pending_acquisition_count",)


@pytest.mark.parametrize(
    ("field", "invalid"), [("state", "finished"), ("reason", "raw-error")]
)
def test_public_observations_refuse_open_state_and_reason_codes(
    field: str, invalid: str
) -> None:
    value = observation().model_dump()
    value[field] = invalid

    with pytest.raises(ValidationError) as rejected:
        ResourceObservation.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == (field,)


def test_unknown_cleanup_preserves_original_outcome_and_invocation_reference() -> None:
    report = ClosureReport(
        invocation_id=INVOCATION_ID,
        resource_id=RESOURCE_ID,
        revision=1,
        state=CleanupOutcome.UNKNOWN,
        original_outcome=OriginalOutcome.CANCELLED,
        cleanup_outcome=CleanupOutcome.UNKNOWN,
        unresolved_count=1,
        samples=(observation(),),
        cleanup_reference=CleanupReference(invocation_id=INVOCATION_ID),
    )

    restored = ClosureReport.model_validate_json(report.model_dump_json())

    assert restored.original_outcome == "cancelled"
    assert restored.cleanup_outcome == "unknown"
    assert restored.cleanup_reference == CleanupReference(invocation_id=INVOCATION_ID)
    assert "token" not in json.loads(report.model_dump_json())["samples"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        "oversized",
        "missing_reference",
        "foreign_reference",
        "inconsistent_total",
        "closed_with_evidence",
    ],
)
def test_closure_reports_refuse_inconsistent_or_unbounded_recovery_evidence(
    mutation: str,
) -> None:
    value: dict[str, object] = {
        "invocation_id": INVOCATION_ID,
        "resource_id": RESOURCE_ID,
        "revision": 1,
        "state": "unknown",
        "original_outcome": "failed",
        "cleanup_outcome": "unknown",
        "unresolved_count": 1,
        "samples": (observation(),),
        "cleanup_reference": CleanupReference(invocation_id=INVOCATION_ID),
    }
    mutations: dict[str, dict[str, object]] = {
        "oversized": {"samples": (observation(),) * 33, "unresolved_count": 33},
        "missing_reference": {"cleanup_reference": None},
        "foreign_reference": {
            "cleanup_reference": CleanupReference(invocation_id=ROOT_RESOURCE_ID)
        },
        "inconsistent_total": {"unresolved_count": 0},
        "closed_with_evidence": {"state": "closed", "cleanup_outcome": "closed"},
    }
    value.update(mutations[mutation])

    with pytest.raises(ValidationError) as rejected:
        ClosureReport.model_validate(value)

    assert rejected.value.error_count() >= 1


def test_unknown_process_identity_survives_transport_for_controller_refusal() -> None:
    value = process_registration().model_dump()
    value["process"] = ProcessIdentity(
        host_id=None,
        boot_id=None,
        pid_namespace=None,
        pid=100,
        start_time=0,
        leads_group=True,
    )
    registration = ProcessRegistration.model_validate(value)

    restored = ProcessRegistration.model_validate_json(registration.model_dump_json())

    assert restored.process.host_id is None
    assert restored.process.boot_id is None
    assert restored.process.pid_namespace is None


def test_acquisition_refuses_a_process_bound_to_a_different_operation() -> None:
    value = {
        "token": reservation_token(),
        "operation_id": "9" * 32,
        "revision": 1,
        "registration": process_registration(),
    }

    with pytest.raises(ValidationError) as rejected:
        Acquisition.model_validate(value)

    assert "operation" in str(rejected.value)


def test_activation_result_is_not_a_closure_or_execution_authority_handle() -> None:
    value = {
        "token": reservation_token(),
        "acquisition_operation_id": OPERATION_ID,
        "revision": 1,
        "result": "closed",
    }

    with pytest.raises(ValidationError) as rejected:
        ActivationAcknowledgement.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == ("result",)


@pytest.mark.parametrize(
    ("model", "field", "encoded"),
    [
        (ReservationRequest, "kind", b"process_domain"),
        (ProcessLaunch, "domain", b"detached"),
        (ProcessLaunch, "stdout", b"capture"),
        (ResourceObservation, "state", b"unknown"),
        (ResourceObservation, "reason", b"effect_unsettled"),
        (ActivationAcknowledgement, "result", b"active"),
        (ClosureReport, "original_outcome", b"complete"),
        (ClosureReport, "cleanup_outcome", b"closed"),
    ],
)
def test_closed_codes_refuse_byte_string_coercion(
    model: type[ReservationRequest]
    | type[ProcessLaunch]
    | type[ResourceObservation]
    | type[ActivationAcknowledgement]
    | type[ClosureReport],
    field: str,
    encoded: bytes,
) -> None:
    payloads: dict[type[object], dict[str, object]] = {
        ReservationRequest: {"operation_id": OPERATION_ID, "kind": "process_domain"},
        ProcessLaunch: process_launch().model_dump(),
        ResourceObservation: observation().model_dump(),
        ActivationAcknowledgement: {
            "token": reservation_token(),
            "acquisition_operation_id": OPERATION_ID,
            "revision": 1,
            "result": "active",
        },
        ClosureReport: {
            "invocation_id": INVOCATION_ID,
            "resource_id": None,
            "revision": 1,
            "state": "closed",
            "original_outcome": "complete",
            "cleanup_outcome": "closed",
            "unresolved_count": 0,
            "samples": (),
            "cleanup_reference": None,
        },
    }
    value = payloads[model]
    value[field] = encoded

    with pytest.raises(ValidationError) as rejected:
        model.model_validate(value)

    assert rejected.value.errors()[0]["loc"] == (field,)
