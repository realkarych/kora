from typing import Annotated, Final, Literal, Self, cast

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from kora.ir.graph_json import GraphValueRejected, TextBudget, render_object
from kora.ir.scalars import Digest
from kora.protocols.boundary import BoundaryModel
from kora.protocols.deadline_records import DeadlineEnvelope
from kora.protocols.history import GrantHistorySnapshot
from kora.protocols.identity import (
    Count,
    GrantBinding,
    Handle,
    Identifier,
    Instant,
    ProcessIdentity,
    object_size,
)
from kora.protocols.storage_models import ContentRef
from kora.protocols.worker_claim_wire import (
    CLAIM_PART_JOIN_BYTES,
    MAX_REQUEST_DESCRIPTOR_BYTES,
    MAX_SYSTEM_BYTES,
    ClaimPartition,
    ClaimWireRejected,
    PreparedClaimPart,
    prepare_claim_part,
)

IDENTIFIER_BYTES: Final[int] = 2 + 2 * 256
PROCESS_BYTES: Final[int] = object_size(
    {
        "host_id": IDENTIFIER_BYTES,
        "boot_id": IDENTIFIER_BYTES,
        "pid_namespace": IDENTIFIER_BYTES,
        "pid": 10,
        "start_time": 19,
        "leads_group": 5,
    }
)
HANDLE_BYTES: Final[int] = object_size(
    {
        "runtime": IDENTIFIER_BYTES,
        "attempt_id": IDENTIFIER_BYTES,
        "execution_domain_id": IDENTIFIER_BYTES,
        "host_hint": IDENTIFIER_BYTES,
        "data": 1_048_576,
    }
)
DEADLINE_BYTES: Final[int] = object_size(
    {
        "version": 1,
        "value": object_size(
            {
                "at": 29,
                "scope_kind": 10,
                "scope_id": IDENTIFIER_BYTES,
                "clock_uncertainty_us": 22,
                "grace_seconds": 2,
            }
        ),
    }
)
GRANT_BYTES: Final[int] = object_size(
    {
        "dispatcher_id": IDENTIFIER_BYTES,
        "worker_id": IDENTIFIER_BYTES,
        "incarnation_id": IDENTIFIER_BYTES,
        "lease_epoch": 19,
        "execution_domain_id": IDENTIFIER_BYTES,
        "process": PROCESS_BYTES,
    }
)
HISTORY_BYTES: Final[int] = object_size(
    {
        "version": 1,
        "namespace_id": IDENTIFIER_BYTES,
        "attempt_id": IDENTIFIER_BYTES,
        "execution_domain_id": IDENTIFIER_BYTES,
        "upper_epoch": 19,
        "count": 19,
        "digest": 66,
    }
)
FIXED_SYSTEM_BYTES: Final[int] = object_size(
    {
        "attempt_id": IDENTIFIER_BYTES,
        "run_id": IDENTIFIER_BYTES,
        "namespace_id": IDENTIFIER_BYTES,
        "node_path": 2 + 2 * 1024,
        "execution_domain_id": IDENTIFIER_BYTES,
        "dispatcher_id": IDENTIFIER_BYTES,
        "mode": 9,
        "lease_epoch": 19,
        "lease_until": 29,
        "deadline": DEADLINE_BYTES,
        "process": PROCESS_BYTES,
        "current_grant": GRANT_BYTES,
        "grant_history": HISTORY_BYTES,
        "execution_request": MAX_REQUEST_DESCRIPTOR_BYTES,
        "handle": HANDLE_BYTES,
        "acknowledged_segment_offset": 19,
        "permitted_actions": 0,
    }
)
MAX_ACTIONS_BYTES: Final[int] = (
    MAX_SYSTEM_BYTES - CLAIM_PART_JOIN_BYTES - FIXED_SYSTEM_BYTES
)


class ClaimRequestDescriptor(BoundaryModel):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    digest: Digest
    storage_ref: ContentRef
    payload_state: Literal["present", "expiring", "expired", "missing"]
    retention_class: Literal["transcripts", "artifact_payloads", "combined"]

    @model_validator(mode="after")
    def complete_descriptor_fits(self) -> Self:
        try:
            render_object(
                self.model_dump(mode="json"),
                TextBudget(MAX_REQUEST_DESCRIPTOR_BYTES, ascii_only=False),
            )
        except GraphValueRejected:
            raise ClaimWireRejected() from None
        return self


class ClaimSystem(BoundaryModel):
    attempt_id: Identifier
    run_id: Identifier
    namespace_id: Identifier
    node_path: Annotated[StrictStr, Field(pattern=r"^[\x21-\x7e]{1,1024}$")]
    execution_domain_id: Identifier
    dispatcher_id: Identifier
    mode: Literal["execute", "recover"]
    lease_epoch: Count
    lease_until: Instant
    deadline: DeadlineEnvelope
    process: ProcessIdentity | None
    current_grant: GrantBinding
    grant_history: GrantHistorySnapshot
    execution_request: ClaimRequestDescriptor | None
    handle: Handle | None
    acknowledged_segment_offset: Count
    permitted_actions: tuple[StrictStr, ...]

    @field_validator("permitted_actions", mode="before")
    @classmethod
    def actions_preserve_recovery_reserve(cls, value: object) -> object:
        if (
            type(value) not in (list, tuple)
            or len(cast(list[object], value)) * 3 + 1 > MAX_ACTIONS_BYTES
        ):
            raise ClaimWireRejected()
        items = cast(list[object] | tuple[object, ...], value)
        if not all(type(item) is str for item in items):
            raise ClaimWireRejected()
        try:
            render_object(
                {"v": list(items)}, TextBudget(MAX_ACTIONS_BYTES + 6, ascii_only=False)
            )
        except GraphValueRejected:
            raise ClaimWireRejected() from None
        return value

    @model_validator(mode="after")
    def retained_bindings_agree(self) -> Self:
        grant, history = self.current_grant, self.grant_history
        expected_process = (
            None if self.process is None else self.process.model_dump(mode="json")
        )
        actual_process = (
            None if grant.process is None else grant.process.model_dump(mode="json")
        )
        if (
            grant.dispatcher_id != self.dispatcher_id
            or grant.lease_epoch != self.lease_epoch
            or grant.execution_domain_id != self.execution_domain_id
            or expected_process != actual_process
            or history.namespace_id != self.namespace_id
            or history.attempt_id != self.attempt_id
            or history.execution_domain_id != self.execution_domain_id
            or history.upper_epoch != self.lease_epoch
            or history.count == 0
        ):
            raise ClaimWireRejected()
        if self.handle is not None and (
            self.handle.attempt_id != self.attempt_id
            or self.handle.execution_domain_id != self.execution_domain_id
        ):
            raise ClaimWireRejected()
        deadline = self.deadline.value
        if deadline is not None and (
            deadline.scope_kind == "attempt"
            and deadline.scope_id != self.attempt_id
            or deadline.scope_kind == "run"
            and deadline.scope_id != self.run_id
        ):
            raise ClaimWireRejected()
        return self


def prepare_claim_system(value: ClaimSystem) -> PreparedClaimPart:
    return prepare_claim_part(ClaimPartition.SYSTEM, value.model_dump(mode="json"))
