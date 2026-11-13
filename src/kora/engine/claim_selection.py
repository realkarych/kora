from dataclasses import dataclass
from datetime import datetime

from kora.cel_eval.errors import raise_safe
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.engine.claim_snapshot import PreparedClaimSnapshot
from kora.engine.claim_timing import ClaimTimeout, prepare_claim_timeout
from kora.engine.deadline_records import DecodedDeadline, decode_deadline_record
from kora.engine.grant_history import PreparedHistory
from kora.protocols.identity import Handle
from kora.protocols.worker_claim_system import (
    ClaimRequestDescriptor,
    ClaimSystem,
    prepare_claim_system,
)
from kora.protocols.worker_claim_wire import PreparedClaimPart, claim_response


@dataclass(frozen=True, slots=True, repr=False)
class SelectedClaim:
    snapshot: PreparedClaimSnapshot
    history: PreparedHistory | None
    deadline: DecodedDeadline
    handle: Handle | None
    request: ClaimRequestDescriptor | None
    run_timeout: ClaimTimeout
    attempt_timeout: ClaimTimeout


def prepare_selected_claim(
    snapshot: PreparedClaimSnapshot,
    history: PreparedHistory | None,
    legacy_deadline: datetime | None,
) -> SelectedClaim:
    values = snapshot.sources.values
    try:
        return SelectedClaim(
            snapshot,
            history,
            decode_deadline_record(values["deadline_binding"], legacy_deadline),
            None
            if values["handle"] is None
            else Handle.model_validate(values["handle"]),
            None
            if values["execution_request"] is None
            else ClaimRequestDescriptor.model_validate(values["execution_request"]),
            prepare_claim_timeout(snapshot.material.run_timeout),
            prepare_claim_timeout(snapshot.material.attempt_timeout),
        )
    except ValueError:
        del snapshot, history, values
        raise_safe(ClaimSourcesRejected())


@dataclass(frozen=True, slots=True, repr=False)
class ClaimDelivery:
    immutable: PreparedClaimPart
    policy: PreparedClaimPart
    system: ClaimSystem

    def encode(self) -> bytes:
        return claim_response(
            self.immutable, self.policy, prepare_claim_system(self.system)
        )
