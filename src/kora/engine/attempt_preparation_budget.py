from dataclasses import dataclass

from kora.db.native_values import NAMESPACE_POLICY_NATIVE_LIMIT
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    ExecutionContractReadLimits,
    execution_contract_read_reservation,
)
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.ir.cost import PriceRevision
from kora.ir.diagnostic_collection import MAX_DIAGNOSTIC_BYTES
from kora.protocols.worker_claim_wire import (
    MAX_IMMUTABLE_BYTES,
    MAX_NODE_POLICY_BYTES,
    MAX_POLICY_BYTES,
    ClaimWireRejected,
)


@dataclass(frozen=True, slots=True)
class AttemptPreparationLimits:
    graph: ExecutionContractReadLimits
    owned_units: int
    retained_units: int
    work_units: int

    def __post_init__(self) -> None:
        if (
            type(self.owned_units) is not int
            or self.owned_units < 0
            or any(
                type(value) is not int or value <= 0
                for value in (self.retained_units, self.work_units)
            )
        ):
            raise ClaimWireRejected()


@dataclass(frozen=True, slots=True)
class AttemptPreparationReservation:
    retained_units: int
    work_units: int


def _typed_units(width: int) -> int:
    return 292 * width + 45


def attempt_preparation_reservation(
    checked: CheckedExecutionContract, limits: AttemptPreparationLimits
) -> AttemptPreparationReservation:
    execution_contract_read_reservation(limits.graph)
    ir, contract = len(checked.canonical_ir), len(checked.canonical_contract)
    if (
        ir + contract > limits.graph.identity.max_encoded_bytes
        or ir > limits.graph.resources.logical.ir_bytes
    ):
        raise ClaimWireRejected()
    namespace = NAMESPACE_POLICY_NATIVE_LIMIT
    node = MAX_NODE_POLICY_BYTES
    immutable = MAX_IMMUTABLE_BYTES
    policy = MAX_POLICY_BYTES
    params = MAX_PARAMS_BYTES
    price = PriceRevision.byte_limit
    logical_input, input_storage, source_wrapper = 2, 25, 37
    decodes = (ir, ir, contract, namespace, namespace, namespace, node, source_wrapper)
    renders = (
        namespace,
        ir,
        params,
        logical_input,
        price,
        immutable,
        node,
        namespace,
        node,
        policy,
        logical_input,
        input_storage,
        source_wrapper,
    )
    typed = (ir, namespace, namespace, node, node)
    auxiliaries = (
        ir + contract,
        params,
        price,
        namespace,
        source_wrapper,
        immutable + policy,
    )
    encodings = (
        params,
        logical_input,
        price,
        immutable,
        node,
        namespace,
        node,
        policy,
        source_wrapper,
    )
    diagnostics = 2 * (_typed_units(MAX_DIAGNOSTIC_BYTES) + 36 * MAX_DIAGNOSTIC_BYTES)
    retained = (
        limits.owned_units
        + 12 * sum(decodes)
        + 36 * sum(renders)
        + sum(_typed_units(width) for width in typed)
        + 12 * sum(auxiliaries)
        + sum(encodings)
        + diagnostics
    )
    operations = (
        *decodes,
        *renders,
        *typed,
        *auxiliaries,
        *encodings,
        MAX_DIAGNOSTIC_BYTES,
        MAX_DIAGNOSTIC_BYTES,
        ir + 11,
    )
    work = sum(32 * (width + 1) ** 2 for width in operations)
    return AttemptPreparationReservation(retained, work)


def admit_attempt_preparation(
    checked: CheckedExecutionContract, limits: AttemptPreparationLimits
) -> AttemptPreparationReservation:
    reservation = attempt_preparation_reservation(checked, limits)
    if (
        reservation.retained_units > limits.retained_units
        or reservation.work_units > limits.work_units
    ):
        raise ClaimWireRejected()
    return reservation
