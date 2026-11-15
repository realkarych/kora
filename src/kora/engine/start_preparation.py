from dataclasses import dataclass

from kora.cel_eval.bounds import MAX_BYTES
from kora.db.graph_resource_owner_reads import MAX_OWNER_ROWS
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import transport_limit
from kora.db.types import DIGEST_LENGTH
from kora.engine.graph_execution_contract import execution_contract_read_reservation
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.ir.diagnostic_collection import MAX_DIAGNOSTIC_BYTES
from kora.ir.standard_schema_policy import SchemaDomain, domain_limits


@dataclass(frozen=True, slots=True)
class StartPreparationLimits:
    snapshot: GraphSnapshotLimits
    cut_work: int
    cut_scratch: int
    schema_work: int
    retained: int
    work: int

    def admit(self) -> None:
        retained, work = start_preparation_reservation(self)
        if retained > self.retained or work > self.work:
            raise GraphValueRejected(GraphValueFailure.BUDGET)


def start_preparation_reservation(limits: StartPreparationLimits) -> tuple[int, int]:
    snapshot = limits.snapshot
    if any(
        type(value) is not int or value <= 0
        for value in (
            snapshot.native_bytes,
            snapshot.owner_text_bytes,
            limits.cut_work,
            limits.cut_scratch,
            limits.schema_work,
            limits.retained,
            limits.work,
        )
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    reader, reader_work = execution_contract_read_reservation(snapshot.graph)
    ir = min(
        snapshot.graph.identity.max_encoded_bytes,
        snapshot.graph.resources.logical.ir_bytes,
    )
    owners = snapshot.owner_text_bytes + (MAX_OWNER_ROWS + 1) * 64 * 8
    raw = (
        transport_limit(snapshot.graph.identity.max_encoded_bytes, 2)
        + 6 * DIGEST_LENGTH
        + snapshot.graph.physical_input_bytes
        + owners
        + 1024
    )
    profile = domain_limits(SchemaDomain.PARAMETERS)
    source = profile["max_schema_source_bytes"]
    baseline = 356 * MAX_PARAMS_BYTES + 4096
    document = 352 * ir + 45
    diagnostics = 328 * MAX_DIAGNOSTIC_BYTES + 45
    value = (
        16 * (MAX_PARAMS_BYTES + source)
        + profile["max_variable_bytes"]
        + profile["max_auxiliary_entries"]
    )
    cut = 16 * MAX_PARAMS_BYTES + limits.cut_scratch + 48 * MAX_BYTES
    cel = document + 64 * limits.schema_work + 64 * MAX_BYTES
    retained = baseline + max(
        snapshot.native_bytes + owners + raw,
        raw + reader,
        raw + reader + document + diagnostics,
        raw + reader + ir + value,
        raw + reader + ir + cut,
        raw + reader + ir + cel,
        2 * (MAX_BYTES + diagnostics + ir + 1024),
    )
    work = (
        snapshot.native_bytes
        + owners
        + raw
        + reader_work
        + 256 * (ir + 1) ** 2
        + 8 * (MAX_PARAMS_BYTES + source + 1) ** 2
        + profile["max_validation_score"]
        + 32 * (MAX_PARAMS_BYTES + 1) ** 2
        + limits.cut_work
        + limits.schema_work
        + 128 * (MAX_BYTES + 1) ** 2
        + 64 * (MAX_DIAGNOSTIC_BYTES + 1) ** 2
        + 4096
    )
    return retained, work
