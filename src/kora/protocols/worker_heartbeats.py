from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator

from kora.ir.diagnostics import closed_fields
from kora.ir.scalars import Digest
from kora.protocols.boundary import BoundaryModel
from kora.protocols.evidence import ClosureEvidence
from kora.protocols.history import GrantHistorySnapshot
from kora.protocols.identity import Count, Identifier, Instant
from kora.protocols.runtime import Liveness, RuntimeStatus
from kora.protocols.vcs import Binding, BindingRevision, CredentialMode

HeartbeatAction = Literal["seal_request", "observe", "retire", "report_result"]


class HeartbeatRecord(BoundaryModel):
    @model_validator(mode="before")
    @classmethod
    def require_known_fields(cls, value: object) -> object:
        return closed_fields(value, cls.model_fields)


class PublicationExpectation(HeartbeatRecord):
    binding: Binding
    binding_revision: BindingRevision
    credential_mode: CredentialMode
    operation_id: Identifier
    prepared_digest: Digest
    destination_digest: Digest


class WorkerHeartbeat(HeartbeatRecord):
    lease_epoch: Count
    dispatcher_id: Identifier
    observation: RuntimeStatus | None = None
    counters: Liveness | None = None
    evidence: ClosureEvidence | None = None
    publication: PublicationExpectation | None = None
    grant_history_complete: GrantHistorySnapshot | None = None


class HeartbeatResponse(HeartbeatRecord):
    desired_state: Literal["run", "cancel"]
    lease_until: Instant
    mode: Literal["execute", "recover"]
    permitted_actions: tuple[HeartbeatAction, ...]
    grant_history: GrantHistorySnapshot | None = None
    grant_history_verified: GrantHistorySnapshot | None
    publication: PublicationExpectation | None = None


class RuntimeEvidenceDetails(HeartbeatRecord):
    reason: Literal["evidence_unbound", "evidence_conflict"]
    item: Literal["runtime"]


class DispatcherEvidenceDetails(HeartbeatRecord):
    reason: Literal["evidence_unbound", "evidence_conflict"]
    item: Literal["dispatchers"]
    index: Annotated[StrictInt, Field(ge=0, le=127)]
    dispatcher_id: Identifier
    lease_epoch: Count
