import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import raise_safe
from kora.db.admission_snapshot_reads import (
    AdmissionCandidateBinding,
    read_admission_candidate,
    read_admission_price_id,
)
from kora.db.admission_waits import open_wait_statement
from kora.db.attempt_capacity import GraphCapSource, read_graph_cap_source
from kora.db.claim_snapshot_reads import (
    POLICY_ROW_BYTES,
    PRICE_ROW_BYTES,
    AcquiredClaimPolicy,
    AcquiredClaimPrice,
    acquire_claim_policy,
    acquire_claim_price,
)
from kora.db.connection import (
    SNAPSHOT_NATIVE_ERRORS,
    Database,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    require_snapshot_time,
)
from kora.db.graph_resource_owner_reads import MAX_OWNER_ROWS
from kora.db.graph_value_json import GraphValueRejected
from kora.db.graph_version_reads import transport_limit
from kora.db.identifiers import is_identifier
from kora.db.run_input_reads import (
    RunInputField,
    RunInputReadLimits,
    acquire_run_input_on_connection,
)
from kora.db.tables import wait
from kora.db.types import DIGEST_LENGTH, IDENTIFIER_LENGTH, NAME_LENGTH, PATH_LENGTH
from kora.engine.graph_execution_contract import (
    ExecutionContractReadLimits,
    execution_contract_read_reservation,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_snapshot import (
    AcquiredGraphSnapshot,
    GraphSnapshotLimits,
    acquire_graph_snapshot,
)
from kora.engine.locks import COMPONENT
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

ACQUIRED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.admission_snapshot.acquired",
        COMPONENT,
        Level.DEBUG,
        "admission snapshot acquisition completed",
        {"acquired": FieldKind.BOOLEAN},
    )
)
OWNER_FIXED_BYTES: int = (MAX_OWNER_ROWS + 1) * 64 * 8
SCALAR_FIXED_BYTES: int = (
    4 * (PATH_LENGTH + 12 * IDENTIFIER_LENGTH + 2 * NAME_LENGTH) + 128 * 8
)


class AdmissionSnapshotRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("admission sources are unavailable or invalid")


class AdmissionCandidateUnavailable(AdmissionSnapshotRejected):
    pass


@dataclass(frozen=True, slots=True)
class AdmissionSnapshotLimits:
    graph: ExecutionContractReadLimits
    inputs: RunInputReadLimits
    node_row: int
    graph_native_bytes: int
    owner_text_bytes: int
    native_bytes: int
    raw_bytes: int

    def reservation(self) -> tuple[int, int]:
        if any(
            type(value) is not int or not 0 < value <= maximum
            for value, maximum in (
                (self.inputs.parent, 2**63 - 1),
                (self.inputs.projection, MAX_PARAMS_BYTES),
                (self.inputs.params, MAX_PARAMS_BYTES),
                (self.node_row, 2**63 - 1),
                (self.graph_native_bytes, 2**63 - 1),
                (self.owner_text_bytes, 2**63 - 1),
                (self.native_bytes, 2**63 - 1),
                (self.raw_bytes, 2**63 - 1),
            )
        ):
            raise AdmissionSnapshotRejected()
        execution_contract_read_reservation(self.graph)
        fixed = PRICE_ROW_BYTES + POLICY_ROW_BYTES + SCALAR_FIXED_BYTES
        owners = self.owner_text_bytes + OWNER_FIXED_BYTES
        native = (
            fixed
            + 2 * (self.graph_native_bytes + owners)
            + self.node_row
            + 2 * self.inputs.parent
            + self.inputs.projection
            + self.inputs.params
        )
        raw = (
            fixed
            + 2
            * (
                transport_limit(self.graph.identity.max_encoded_bytes, 2)
                + 6 * DIGEST_LENGTH
                + self.graph.physical_input_bytes
                + owners
            )
            + self.inputs.params
        )
        return native, raw

    def admit(self) -> None:
        native, raw = self.reservation()
        if native > self.native_bytes or raw > self.raw_bytes:
            raise AdmissionSnapshotRejected()


@dataclass(frozen=True, slots=True, repr=False)
class AcquiredAdmissionSnapshot:
    candidate: AdmissionCandidateBinding
    graph: AcquiredGraphSnapshot
    cap_source: GraphCapSource
    cap_graph: AcquiredGraphSnapshot
    params: bytes
    price_table_id: str
    price: AcquiredClaimPrice
    policy: AcquiredClaimPolicy
    wait_id: str | None
    observed_at: datetime
    periodic_recheck: bool


async def _wait_owner(
    connection: AsyncConnection,
    candidate: AdmissionCandidateBinding,
    observed_at: datetime,
    periodic_recheck: bool,
) -> str | None:
    with await connection.execute(
        open_wait_statement(connection.dialect.name, candidate.node_id)
    ) as result:
        rows = result.all()
    if candidate.status != "waiting":
        if rows:
            raise AdmissionCandidateUnavailable()
        return None
    if len(rows) != 1:
        raise AdmissionCandidateUnavailable()
    identity, kind, attempt_id = rows[0]
    if (
        type(identity) is not str
        or not is_identifier(identity)
        or kind != "admission"
        or attempt_id is not None
    ):
        raise AdmissionCandidateUnavailable()
    if periodic_recheck:
        with await connection.execute(
            select(wait.c.wake_at).where(wait.c.id == identity)
        ) as result:
            wake = result.scalar_one_or_none()
        if wake is None or wake > observed_at:
            raise AdmissionCandidateUnavailable()
    return identity


async def _acquire_admission_snapshot(
    database: Database,
    node_id: str,
    limits: AdmissionSnapshotLimits,
    observed_at: datetime,
    periodic_recheck: bool,
    deadline: float,
) -> AcquiredAdmissionSnapshot:
    if (
        type(node_id) is not str
        or not is_identifier(node_id)
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or type(periodic_recheck) is not bool
        or type(deadline) is not float
        or not math.isfinite(deadline)
    ):
        raise AdmissionSnapshotRejected()
    limits.admit()
    require_snapshot_time(deadline)

    def before_statement(connection: Connection, *arguments: object) -> None:
        require_snapshot_time(deadline)

    async with database.read_snapshot(deadline=deadline) as connection:
        event.listen(
            connection.sync_connection, "before_cursor_execute", before_statement
        )
        try:
            candidate = await read_admission_candidate(
                connection,
                node_id,
                node_row=limits.node_row,
                run_row=limits.inputs.parent,
            )
            if candidate is None:
                raise AdmissionCandidateUnavailable()
            wait_id = await _wait_owner(
                connection, candidate, observed_at, periodic_recheck
            )
            scope = candidate.run
            price_id = await read_admission_price_id(
                connection, scope.namespace_id, observed_at
            )
            cap_source = await read_graph_cap_source(connection, scope.graph_id)
            if (
                price_id is None
                or cap_source is None
                or type(cap_source.number) is not int
                or not 0 < cap_source.number < 2**31
            ):
                raise AdmissionSnapshotRejected()
            graph_limits = GraphSnapshotLimits(
                limits.graph, limits.graph_native_bytes, limits.owner_text_bytes
            )
            graph = await acquire_graph_snapshot(
                connection,
                GraphResourceScope(
                    scope.namespace_id, scope.graph_id, scope.version_id
                ),
                graph_limits,
            )
            cap_graph = (
                graph
                if cap_source.version_id == scope.version_id
                else await acquire_graph_snapshot(
                    connection,
                    GraphResourceScope(
                        scope.namespace_id, scope.graph_id, cap_source.version_id
                    ),
                    graph_limits,
                )
            )
            params = await acquire_run_input_on_connection(
                connection, scope, RunInputField.PARAMS, limits.inputs
            )
            if params is None:
                raise AdmissionCandidateUnavailable()
            price = await acquire_claim_price(connection, scope.namespace_id, price_id)
            policy = await acquire_claim_policy(connection, scope.namespace_id)
            snapshot = AcquiredAdmissionSnapshot(
                candidate,
                graph,
                cap_source,
                cap_graph,
                params,
                price_id,
                price,
                policy,
                wait_id,
                observed_at,
                periodic_recheck,
            )
        finally:
            event.remove(
                connection.sync_connection,
                "before_cursor_execute",
                before_statement,
            )
    return snapshot


async def acquire_admission_snapshot(
    database: Database,
    node_id: str,
    limits: AdmissionSnapshotLimits,
    *,
    observed_at: datetime,
    periodic_recheck: bool,
    deadline: float,
) -> AcquiredAdmissionSnapshot:
    acquired = False
    try:
        result = await _acquire_admission_snapshot(
            database, node_id, limits, observed_at, periodic_recheck, deadline
        )
        acquired = True
        return result
    except AdmissionCandidateUnavailable:
        raise
    except DatabaseSnapshotRejected as error:
        if error.reason is DatabaseSnapshotFailure.DEADLINE:
            raise_safe(AdmissionCandidateUnavailable())
        raise_safe(AdmissionSnapshotRejected())
    except GraphValueRejected:
        raise_safe(AdmissionCandidateUnavailable())
    except (ValueError, *SNAPSHOT_NATIVE_ERRORS):
        del database, node_id, limits, observed_at
        raise_safe(AdmissionSnapshotRejected())
    finally:
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.logger(COMPONENT).emit(ACQUIRED, acquired=acquired)
