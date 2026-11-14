import asyncio
from dataclasses import dataclass
from datetime import datetime

from kora.cel_eval.errors import raise_safe
from kora.db.claim_snapshot_reads import PRICE_NATIVE_BYTES
from kora.db.claim_source_values import decode_claim_source
from kora.db.connection import (
    SNAPSHOT_NATIVE_ERRORS,
    Database,
    DatabaseSnapshotRejected,
    clear_snapshot_error,
    require_snapshot_time,
)
from kora.db.native_values import NAMESPACE_POLICY_NATIVE_LIMIT
from kora.db.types import DIGEST_LENGTH
from kora.engine.admission_snapshot import (
    AcquiredAdmissionSnapshot,
    AdmissionSnapshotLimits,
    acquire_admission_snapshot,
)
from kora.engine.attempt_capacity import PreparedGraphCaps, prepare_graph_caps
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
    execution_contract_read_reservation,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_snapshot import AcquiredGraphSnapshot
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import COMPONENT
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.readiness import PreparedReadiness, prepare_readiness
from kora.engine.run_input_values import decode_run_input_value
from kora.ir.cost import PriceRevision
from kora.ir.diagnostic_collection import MAX_DIAGNOSTIC_BYTES
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.admission_preparation.completed",
        COMPONENT,
        Level.DEBUG,
        "admission source preparation completed",
        {"prepared": FieldKind.BOOLEAN},
    )
)


class AdmissionPreparationRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("admission sources cannot be prepared")


@dataclass(frozen=True, slots=True)
class AdmissionPreparationLimits:
    snapshot: AdmissionSnapshotLimits
    retained_units: int
    work_units: int


@dataclass(frozen=True, slots=True)
class AdmissionPreparationReservation:
    retained_units: int
    work_units: int


@dataclass(frozen=True, slots=True, repr=False)
class PreparedAdmissionSnapshot:
    acquired: AcquiredAdmissionSnapshot
    checked: CheckedExecutionContract
    cap_checked: CheckedExecutionContract
    readiness: PreparedReadiness
    caps: PreparedGraphCaps
    price: PriceRevision
    policy: NamespacePolicyConfiguration
    reservation: AdmissionPreparationReservation


def _typed_units(width: int) -> int:
    return 292 * width + 45


def admission_preparation_reservation(
    limits: AdmissionPreparationLimits,
) -> AdmissionPreparationReservation:
    if any(
        type(value) is not int or value <= 0
        for value in (limits.retained_units, limits.work_units)
    ):
        raise AdmissionPreparationRejected()
    snapshot = limits.snapshot
    snapshot.admit()
    native, raw = snapshot.reservation()
    graph_retained, graph_work = execution_contract_read_reservation(snapshot.graph)
    ir = min(
        snapshot.graph.identity.max_encoded_bytes,
        snapshot.graph.resources.logical.ir_bytes,
    )
    namespace, params = NAMESPACE_POLICY_NATIVE_LIMIT, snapshot.inputs.params
    price = PRICE_NATIVE_BYTES + 6
    decodes = (ir, ir, params, price, namespace + 6, namespace, namespace, namespace)
    renders = (
        ir,
        ir,
        params,
        price,
        namespace + 6,
        namespace,
        namespace,
        namespace,
        namespace,
    )
    typed = (ir, ir, price)
    auxiliary = (
        ir,
        ir,
        params,
        price,
        price,
        namespace,
        namespace,
        2 * DIGEST_LENGTH,
        128,
    )
    diagnostic = MAX_DIAGNOSTIC_BYTES
    retained = (
        512
        + native
        + raw
        + 2 * graph_retained
        + 12 * sum(decodes)
        + 36 * sum(renders)
        + sum(_typed_units(width) for width in typed)
        + 12 * sum(auxiliary)
        + _typed_units(diagnostic)
        + 36 * diagnostic
    )
    work = (
        512
        + 2 * graph_work
        + sum(
            32 * (width + 1) ** 2
            for width in (
                *decodes,
                *renders,
                *typed,
                *auxiliary,
                diagnostic,
                diagnostic,
            )
        )
    )
    return AdmissionPreparationReservation(retained, work)


def _checked(
    acquired: AcquiredGraphSnapshot, limits: AdmissionSnapshotLimits
) -> CheckedExecutionContract:
    graph = acquired.graph
    return check_detached_execution_contract(
        graph,
        GraphVersionPin(
            graph.graph_id, graph.id, graph.digests[1].decode(graph.encoding)
        ),
        GraphResourceScope(graph.namespace_id, graph.graph_id, graph.id),
        acquired.physical,
        acquired.owners,
        limits=limits.graph,
    )


def _prepare(
    acquired: AcquiredAdmissionSnapshot,
    limits: AdmissionPreparationLimits,
    reservation: AdmissionPreparationReservation,
) -> PreparedAdmissionSnapshot:
    checked = _checked(acquired.graph, limits.snapshot)
    cap_checked = (
        checked
        if acquired.cap_graph is acquired.graph
        else _checked(acquired.cap_graph, limits.snapshot)
    )
    maximum = limits.snapshot.graph.resources.logical.ir_bytes
    readiness = prepare_readiness(checked, max_ir_bytes=maximum)
    caps = prepare_graph_caps(cap_checked, max_ir_bytes=maximum)
    decode_run_input_value(acquired.params, max_bytes=limits.snapshot.inputs.params)
    encoding = acquired.graph.physical.encoding
    policy = NamespacePolicyConfiguration.prepare(
        acquired.candidate.run.namespace_id,
        acquired.policy.revision,
        decode_claim_source(acquired.policy.payload, encoding),
    )
    price = PriceRevision.model_validate(
        {
            "id": acquired.price_table_id,
            "namespace_id": acquired.price.namespace_id,
            "effective_from": acquired.price.effective_from.decode(encoding),
            "version": 1,
            "normalization_version": 1,
            "prices": decode_claim_source(acquired.price.prices, encoding),
        }
    )
    return PreparedAdmissionSnapshot(
        acquired, checked, cap_checked, readiness, caps, price, policy, reservation
    )


async def prepare_admission_snapshot(
    database: Database,
    node_id: str,
    limits: AdmissionPreparationLimits,
    *,
    observed_at: datetime,
    periodic_recheck: bool,
    deadline: float,
) -> PreparedAdmissionSnapshot:
    acquired: AcquiredAdmissionSnapshot | None = None
    prepared: PreparedAdmissionSnapshot | None = None
    completed = False
    try:
        reservation = admission_preparation_reservation(limits)
        if (
            reservation.retained_units > limits.retained_units
            or reservation.work_units > limits.work_units
        ):
            raise AdmissionPreparationRejected()
        acquired = await acquire_admission_snapshot(
            database,
            node_id,
            limits.snapshot,
            observed_at=observed_at,
            periodic_recheck=periodic_recheck,
            deadline=deadline,
        )
        prepared = _prepare(acquired, limits, reservation)
        await asyncio.sleep(0)
        require_snapshot_time(deadline)
        completed = True
        return prepared
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
        DatabaseSnapshotRejected,
        *SNAPSHOT_NATIVE_ERRORS,
    ):
        raise_safe(AdmissionPreparationRejected())
    finally:
        del acquired, prepared, database, node_id, limits
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.logger(COMPONENT).emit(PREPARED, prepared=completed)
