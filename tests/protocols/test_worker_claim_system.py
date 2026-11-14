from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from kora.engine.deadline_records import prepare_deadline_record
from kora.ir.json import JsonObject
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.history import GrantHistorySnapshot
from kora.protocols.identity import (
    MAX_COUNT,
    MAX_PID,
    GrantBinding,
    Handle,
    ProcessIdentity,
)
from kora.protocols.storage_models import ContentRef
from kora.protocols.worker_claim_policy import (
    prepare_claim_policy,
    prepare_namespace_policy,
    prepare_node_policy,
)
from kora.protocols.worker_claim_system import (
    DEADLINE_BYTES,
    FIXED_SYSTEM_BYTES,
    GRANT_BYTES,
    HANDLE_BYTES,
    HISTORY_BYTES,
    MAX_ACTIONS_BYTES,
    PROCESS_BYTES,
    ClaimRequestDescriptor,
    ClaimSystem,
    prepare_claim_system,
)
from kora.protocols.worker_claim_wire import (
    CLAIM_PART_JOIN_BYTES,
    MAX_CLAIM_BYTES,
    MAX_IMMUTABLE_BYTES,
    MAX_NAMESPACE_POLICY_BYTES,
    MAX_NODE_POLICY_BYTES,
    MAX_REQUEST_DESCRIPTOR_BYTES,
    MAX_SYSTEM_BYTES,
    ClaimPartition,
    claim_response,
    prepare_claim_part,
)

IDENTITY = "\\" * 256


def system_values() -> dict[str, object]:
    process = ProcessIdentity(
        host_id=IDENTITY,
        boot_id=IDENTITY,
        pid_namespace=IDENTITY,
        pid=MAX_PID,
        start_time=MAX_COUNT,
        leads_group=False,
    )
    grant = GrantBinding(
        dispatcher_id=IDENTITY,
        worker_id=IDENTITY,
        incarnation_id=IDENTITY,
        lease_epoch=MAX_COUNT,
        execution_domain_id=IDENTITY,
        process=process,
    )
    request = ClaimRequestDescriptor(
        version=1,
        digest="sha256:" + "a" * 64,
        storage_ref=ContentRef.model_validate(
            {
                "version": 1,
                "address": {
                    "version": 1,
                    "storage_identity": {"version": 1, "store_id": IDENTITY},
                    "payload_digest": "sha256:" + "a" * 64,
                },
                "object_incarnation": IDENTITY,
                "total_size": 268_435_456,
            }
        ),
        payload_state="expiring",
        retention_class="artifact_payloads",
    )
    return {
        "attempt_id": IDENTITY,
        "run_id": IDENTITY,
        "namespace_id": IDENTITY,
        "node_path": "\\" * 1024,
        "execution_domain_id": IDENTITY,
        "dispatcher_id": IDENTITY,
        "mode": "execute",
        "lease_epoch": MAX_COUNT,
        "lease_until": datetime.max.replace(tzinfo=UTC),
        "deadline": prepare_deadline_record(
            ExecutionDeadline(
                datetime.max.replace(tzinfo=UTC),
                "subgraph",
                IDENTITY,
                timedelta.max,
                timedelta(seconds=30),
            )
        ),
        "process": process,
        "current_grant": grant,
        "grant_history": GrantHistorySnapshot(
            version=1,
            namespace_id=IDENTITY,
            attempt_id=IDENTITY,
            execution_domain_id=IDENTITY,
            upper_epoch=MAX_COUNT,
            count=MAX_COUNT,
            digest="a" * 64,
        ),
        "execution_request": request,
        "handle": Handle.model_validate(
            {
                "runtime": IDENTITY,
                "attempt_id": IDENTITY,
                "execution_domain_id": IDENTITY,
                "host_hint": IDENTITY,
                "data": {"v": "a" * (1_048_576 - 8)},
            }
        ),
        "acknowledged_segment_offset": MAX_COUNT,
        "permitted_actions": ["a" * (MAX_ACTIONS_BYTES - 4)],
    }


def test_system_closed_maximum_reserves_complete_future_recovery() -> None:
    system = ClaimSystem.model_validate(system_values())

    prepared = prepare_claim_system(system)

    assert system.process is not None
    assert len(system.process.model_dump_json().encode()) == PROCESS_BYTES
    assert len(system.current_grant.model_dump_json().encode()) == GRANT_BYTES
    assert len(system.grant_history.model_dump_json().encode()) == HISTORY_BYTES
    assert len(system.deadline.model_dump_json().encode()) == DEADLINE_BYTES
    assert system.handle is not None
    assert len(system.handle.model_dump_json().encode()) == HANDLE_BYTES
    assert system.execution_request is not None
    request_bytes = len(system.execution_request.model_dump_json().encode())
    assert request_bytes <= MAX_REQUEST_DESCRIPTOR_BYTES
    assert (
        FIXED_SYSTEM_BYTES + MAX_ACTIONS_BYTES + CLAIM_PART_JOIN_BYTES
        == MAX_SYSTEM_BYTES
    )
    assert len(prepared.payload) == MAX_SYSTEM_BYTES - CLAIM_PART_JOIN_BYTES - (
        MAX_REQUEST_DESCRIPTOR_BYTES - request_bytes
    )


@pytest.mark.parametrize("recovery", [False, True])
def test_system_actions_cannot_borrow_absent_recovery_reserves(recovery: bool) -> None:
    values = system_values()
    if not recovery:
        values["handle"] = values["execution_request"] = None
    values["permitted_actions"] = ["a" * (MAX_ACTIONS_BYTES - 3)]

    with pytest.raises(ValidationError):
        ClaimSystem.model_validate(values)


@pytest.mark.parametrize(
    "changed", ["process", "grant", "history", "handle", "deadline", "extra"]
)
def test_system_refuses_conflicting_retained_identity(changed: str) -> None:
    values = system_values()
    values["permitted_actions"] = ["report_result"]
    if changed == "process":
        values["process"] = ProcessIdentity(
            host_id=IDENTITY,
            boot_id=IDENTITY,
            pid_namespace=IDENTITY,
            pid=MAX_PID,
            start_time=MAX_COUNT,
            leads_group=True,
        )
    elif changed == "grant":
        values["dispatcher_id"] = "another"
    elif changed == "history":
        values["namespace_id"] = "another"
    elif changed == "handle":
        values["attempt_id"] = "another"
    elif changed == "deadline":
        values["deadline"] = None
    else:
        values["unrecognized"] = True

    with pytest.raises(ValidationError):
        ClaimSystem.model_validate(values)


def test_complete_claim_fits_declared_transport_bound() -> None:
    immutable_value: JsonObject = {"resolved_config": {"prompt": ""}}
    wrapper = len(prepare_claim_part(ClaimPartition.IMMUTABLE, immutable_value).payload)
    immutable_value["resolved_config"] = {
        "prompt": "a" * (MAX_IMMUTABLE_BYTES - wrapper)
    }
    immutable = prepare_claim_part(ClaimPartition.IMMUTABLE, immutable_value)
    node_wrapper = len(prepare_node_policy({"x-data": ""}))
    namespace_wrapper = len(prepare_namespace_policy({"tool_policy": {"x-data": ""}}))
    policy = prepare_claim_policy(
        {
            "tool_policy": {
                "x-data": "a" * (MAX_NAMESPACE_POLICY_BYTES - namespace_wrapper)
            }
        },
        MAX_COUNT,
        prepare_node_policy({"x-data": "a" * (MAX_NODE_POLICY_BYTES - node_wrapper)}),
    )
    system = prepare_claim_system(ClaimSystem.model_validate(system_values()))

    response = claim_response(immutable, policy, system)

    assert len(immutable.payload) == MAX_IMMUTABLE_BYTES
    assert len(response) <= MAX_CLAIM_BYTES
    assert response.startswith(b'{"claim":{')
    assert response.endswith(b"}}")
