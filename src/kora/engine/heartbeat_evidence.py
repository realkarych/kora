from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from kora.protocols.boundary import BoundaryModel
from kora.protocols.evidence import (
    ClosureEvidence,
    DispatcherClosure,
    NormalCompletionQuiescence,
    TerminationReceipt,
)
from kora.protocols.history import record_bytes
from kora.protocols.identity import Count, Identifier, ProcessIdentity, Sha256
from kora.protocols.runtime import RuntimeStatus
from kora.protocols.worker_heartbeats import (
    DispatcherEvidenceDetails,
    RuntimeEvidenceDetails,
)


class ObservationGrant(BoundaryModel):
    worker_id: Identifier
    incarnation_id: Identifier
    dispatcher_id: Identifier
    lease_epoch: Count


class WorkerRecovery(BoundaryModel):
    version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    last_observation: RuntimeStatus | None = None
    observation_grant: ObservationGrant | None = None
    normal_completion: NormalCompletionQuiescence | None = None
    termination: TerminationReceipt | None = None

    @model_validator(mode="after")
    def observation_has_grant(self) -> Self:
        if (self.last_observation is None) != (self.observation_grant is None):
            raise ValueError("the saved observation has no matching grant")
        return self


class HeartbeatRecovery(BoundaryModel):
    reason: Identifier | None = None
    worker: WorkerRecovery | None = None


class HeartbeatGrant(BoundaryModel):
    id: Identifier
    attempt_id: Identifier
    execution_domain_id: Identifier
    worker_id: Identifier
    incarnation_id: Identifier
    lease_epoch: Count
    mode: Literal["execute", "recover"]
    process: ProcessIdentity | None
    closed_at: datetime | None
    evidence: DispatcherClosure | None
    prefix_count: Count
    prefix_digest: Sha256


class HeartbeatEvidenceRejected(ValueError):
    def __init__(
        self, details: DispatcherEvidenceDetails | RuntimeEvidenceDetails
    ) -> None:
        super().__init__("heartbeat closure evidence was refused")
        self.details = details


def accept_heartbeat_evidence(
    before: WorkerRecovery,
    current: HeartbeatGrant,
    evidence: ClosureEvidence | None,
    grants: tuple[HeartbeatGrant, ...],
    count: int,
) -> tuple[WorkerRecovery, tuple[DispatcherClosure, ...], int]:
    if evidence is None:
        return before, (), 0
    after = before
    runtime_changed = 0
    if evidence.runtime is not None:
        item = evidence.runtime
        if (item.attempt_id, item.execution_domain_id) != (
            current.attempt_id,
            current.execution_domain_id,
        ):
            raise HeartbeatEvidenceRejected(
                RuntimeEvidenceDetails(reason="evidence_unbound", item="runtime")
            )
        name = (
            "termination"
            if isinstance(item, TerminationReceipt)
            else "normal_completion"
        )
        prior = getattr(before, name)
        if prior is not None and record_bytes(prior) != record_bytes(item):
            raise HeartbeatEvidenceRejected(
                RuntimeEvidenceDetails(reason="evidence_conflict", item="runtime")
            )
        if prior is None:
            after = before.model_copy(update={name: item})
            runtime_changed = 1
    by_key = {(grant.id, grant.lease_epoch): grant for grant in grants}
    changed: list[DispatcherClosure] = []
    for index, closure in enumerate(evidence.dispatchers):
        item = closure.quiescence
        grant = by_key.get((item.dispatcher_id, item.lease_generation))
        bound = (
            grant is not None
            and len(evidence.dispatchers) <= count
            and (item.attempt_id, item.execution_domain_id, item.worker_incarnation_id)
            == (current.attempt_id, current.execution_domain_id, grant.incarnation_id)
            and (grant.execution_domain_id == current.execution_domain_id)
        )
        if bound and closure.provenance == "runtime_observed_process_exit":
            bound = (
                grant is not None
                and grant.process is not None
                and closure.process is not None
                and (
                    grant.process.leads_group
                    and all(value is not None for value in grant.process.identity_key())
                    and grant.process.identity_key() == closure.process.identity_key()
                    and grant.incarnation_id != current.incarnation_id
                )
            )
        reason: Literal["evidence_unbound", "evidence_conflict"] | None = None
        if not bound or grant is None:
            reason = "evidence_unbound"
        elif grant.closed_at is not None or grant.evidence is not None:
            if (
                grant.evidence is None
                or record_bytes(grant.evidence) != record_bytes(closure)
                or grant.closed_at != item.closed_at
            ):
                reason = "evidence_conflict"
        else:
            changed.append(closure)
        if reason is not None:
            raise HeartbeatEvidenceRejected(
                DispatcherEvidenceDetails(
                    reason=reason,
                    item="dispatchers",
                    index=index,
                    dispatcher_id=item.dispatcher_id,
                    lease_epoch=item.lease_generation,
                )
            )
    return after, tuple(changed), runtime_changed
