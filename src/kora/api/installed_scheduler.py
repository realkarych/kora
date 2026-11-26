from dataclasses import replace
from datetime import timedelta

from kora.db.connection import Database
from kora.db.identifiers import Clock, IdentifierGenerator
from kora.engine.admission_preparation import (
    AdmissionPreparationLimits,
    admission_preparation_reservation,
)
from kora.engine.admission_processing import (
    AdmissionProcessingLimits,
    AdmissionProcessor,
)
from kora.engine.admission_snapshot import AdmissionSnapshotLimits
from kora.engine.attempt_preparation_budget import (
    AttemptPreparationLimits,
    initial_root_preparation_reservation,
)
from kora.engine.claim_material import ClaimRuntimeSelection
from kora.engine.worker_claim_poll import ClaimPollLimits
from kora.ir.deployment_checks import DeploymentSelection
from kora.protocols.runtime import RuntimeCapabilities

SCHEDULER_VARIABLE: str = "KORA_API_SCHEDULER"
FIRST_CLAIM: str = "first-claim"


def initial_admission_limits(claim: ClaimPollLimits) -> AdmissionProcessingLimits:
    source = claim.snapshot
    snapshot = AdmissionSnapshotLimits(
        source.graph,
        source.inputs,
        source.sources.node_row,
        source.graph_native_bytes,
        source.owner_text_bytes,
        1,
        1,
    )
    native, raw = snapshot.reservation()
    preparation = AdmissionPreparationLimits(
        replace(snapshot, native_bytes=native, raw_bytes=raw), 1, 1
    )
    owned = admission_preparation_reservation(preparation)
    max_claim = initial_root_preparation_reservation(
        AttemptPreparationLimits(source.graph, owned.retained_units, 1, 1)
    )
    return AdmissionProcessingLimits(
        replace(
            preparation,
            retained_units=max_claim.retained_units,
            work_units=owned.work_units,
        ),
        source.sources,
        claim.expiry,
        max_claim.work_units,
    )


def installed_root_admission(
    database: Database,
    worker: ClaimPollLimits,
    clock: Clock,
    identifiers: IdentifierGenerator,
    selection: DeploymentSelection,
    capabilities: RuntimeCapabilities,
) -> AdmissionProcessor:
    return AdmissionProcessor(
        database,
        initial_admission_limits(worker),
        ClaimRuntimeSelection("local", "git", capabilities.default_stall_after),
        selection,
        clock,
        identifiers,
        1,
        capabilities.max_concurrent,
        timedelta(seconds=1),
        initial_root_only=True,
    )
