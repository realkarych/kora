from dataclasses import dataclass
from typing import cast

from sqlalchemy import event
from sqlalchemy.engine import Connection

from kora.cel_eval.errors import raise_safe
from kora.db.claim_snapshot_reads import (
    POLICY_ROW_BYTES,
    PRICE_ROW_BYTES,
    AcquiredClaimPolicy,
    AcquiredClaimPrice,
    acquire_claim_policy,
    acquire_claim_price,
)
from kora.db.claim_source_values import ClaimSourcesRejected, decode_claim_source
from kora.db.claim_sources import (
    AcquiredClaimSources,
    ClaimSourceBinding,
    ClaimSourceReadLimits,
    acquire_claim_sources,
)
from kora.db.connection import (
    SNAPSHOT_NATIVE_ERRORS,
    Database,
    DatabaseSnapshotRejected,
    require_snapshot_time,
)
from kora.db.graph_resource_owner_reads import AcquiredGraphResourceOwners
from kora.db.graph_version_reads import AcquiredGraphVersion
from kora.db.run_input_reads import (
    RunInputField,
    RunInputReadLimits,
    acquire_run_input_on_connection,
)
from kora.engine.attempt_inputs import read_attempt_inputs
from kora.engine.claim_material import (
    PreparedClaimMaterial,
    RuntimeSelector,
    prepare_claim_material,
)
from kora.engine.claim_sources import DecodedClaimSources, decode_claim_sources
from kora.engine.graph_execution_contract import (
    ExecutionContractReadLimits,
    check_detached_execution_contract,
    execution_contract_read_reservation,
)
from kora.engine.graph_resource_references import (
    GraphResourceScope,
    NativeGraphResourceTransport,
)
from kora.engine.graph_snapshot import GraphSnapshotLimits, acquire_graph_snapshot
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import COMPONENT
from kora.engine.run_input_reads import ReadRunInput
from kora.engine.run_input_values import decode_run_input_value
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.ir.cost import PriceRevision
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.worker_claim_policy import prepare_claim_policy
from kora.protocols.worker_claim_wire import PreparedClaimPart

ACQUIRED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.claim_snapshot.acquired",
        COMPONENT,
        Level.DEBUG,
        "claim snapshot acquisition completed",
        {"acquired": FieldKind.BOOLEAN},
    )
)


@dataclass(frozen=True, slots=True)
class ClaimSnapshotLimits:
    graph: ExecutionContractReadLimits
    inputs: RunInputReadLimits
    sources: ClaimSourceReadLimits
    graph_native_bytes: int
    owner_text_bytes: int
    native_bytes: int
    raw_bytes: int

    def admit(self) -> None:
        if any(
            type(value) is not int or not 0 < value <= maximum
            for value, maximum in (
                (self.inputs.parent, 2**63 - 1),
                (self.inputs.projection, MAX_PARAMS_BYTES),
                (self.inputs.params, MAX_PARAMS_BYTES),
            )
        ):
            raise ClaimSourcesRejected()
        execution_contract_read_reservation(self.graph)
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.graph_native_bytes,
                self.owner_text_bytes,
                self.native_bytes,
                self.raw_bytes,
            )
        ):
            raise ClaimSourcesRejected()
        fixed = PRICE_ROW_BYTES + POLICY_ROW_BYTES + self.owner_text_bytes + 4096
        native = (
            fixed
            + self.graph_native_bytes
            + self.inputs.parent
            + self.inputs.projection
            + self.inputs.params
            + self.sources.run_row
            + self.sources.node_row
            + self.sources.attempt_row
            + sum(field.native for field in self.sources.fields)
        )
        raw = (
            fixed
            + 4 * self.graph.identity.max_encoded_bytes
            + self.graph.physical_input_bytes
            + self.inputs.params
            + sum(field.transport for field in self.sources.fields)
        )
        if native > self.native_bytes or raw > self.raw_bytes:
            raise ClaimSourcesRejected()


@dataclass(frozen=True, slots=True, repr=False)
class AcquiredClaimSnapshot:
    graph: AcquiredGraphVersion
    physical: NativeGraphResourceTransport
    owners: AcquiredGraphResourceOwners
    params: bytes
    sources: AcquiredClaimSources
    price: AcquiredClaimPrice
    policy: AcquiredClaimPolicy | None


@dataclass(frozen=True, slots=True, repr=False)
class PreparedClaimSnapshot:
    material: PreparedClaimMaterial
    policy: PreparedClaimPart
    sources: DecodedClaimSources
    policy_revision: int | None


async def _acquire_claim_snapshot(
    database: Database,
    binding: ClaimSourceBinding,
    limits: ClaimSnapshotLimits,
    *,
    deadline: float,
) -> AcquiredClaimSnapshot:
    def before_statement(connection: Connection, *arguments: object) -> None:
        require_snapshot_time(deadline)

    limits.admit()
    async with database.read_snapshot(deadline=deadline) as connection:
        event.listen(
            connection.sync_connection, "before_cursor_execute", before_statement
        )
        try:
            sources = await acquire_claim_sources(connection, binding, limits.sources)
            if sources is None:
                raise ClaimSourcesRejected()
            scope = binding.run
            graph = await acquire_graph_snapshot(
                connection,
                GraphResourceScope(
                    scope.namespace_id, scope.graph_id, scope.version_id
                ),
                GraphSnapshotLimits(
                    limits.graph, limits.graph_native_bytes, limits.owner_text_bytes
                ),
            )
            params = await acquire_run_input_on_connection(
                connection, binding.run, RunInputField.PARAMS, limits.inputs
            )
            if params is None:
                raise ClaimSourcesRejected()
            price = await acquire_claim_price(
                connection, binding.run.namespace_id, binding.price_table_id
            )
            if (sources.values[1] is None) != (sources.values[2] is None):
                raise ClaimSourcesRejected()
            policy = (
                await acquire_claim_policy(connection, binding.run.namespace_id)
                if sources.values[1] is None
                else None
            )
            result = AcquiredClaimSnapshot(
                graph.graph,
                graph.physical,
                graph.owners,
                params,
                sources,
                price,
                policy,
            )
        finally:
            event.remove(
                connection.sync_connection,
                "before_cursor_execute",
                before_statement,
            )
    return result


async def acquire_claim_snapshot(
    database: Database,
    binding: ClaimSourceBinding,
    limits: ClaimSnapshotLimits,
    *,
    deadline: float,
) -> AcquiredClaimSnapshot:
    acquired = False
    try:
        result = await _acquire_claim_snapshot(
            database, binding, limits, deadline=deadline
        )
        acquired = True
        return result
    except (ValueError, DatabaseSnapshotRejected, *SNAPSHOT_NATIVE_ERRORS):
        del database, binding, limits
        raise_safe(ClaimSourcesRejected())
    finally:
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.logger(COMPONENT).emit(ACQUIRED, acquired=acquired)


def _native_object(payload: bytes, encoding: str) -> JsonObject:
    value = decode_claim_source(payload, encoding)
    if not isinstance(value, dict):
        raise ClaimSourcesRejected()
    return value


def _prepare_claim_snapshot(
    acquired: AcquiredClaimSnapshot,
    pin: GraphVersionPin,
    selection: RuntimeSelector,
    limits: ClaimSnapshotLimits,
) -> PreparedClaimSnapshot:
    binding = acquired.sources.binding
    scope = binding.run
    checked = check_detached_execution_contract(
        acquired.graph,
        pin,
        GraphResourceScope(scope.namespace_id, scope.graph_id, scope.version_id),
        acquired.physical,
        acquired.owners,
        limits=limits.graph,
    )
    params = ReadRunInput(
        scope,
        RunInputField.PARAMS,
        decode_run_input_value(acquired.params, max_bytes=limits.inputs.params),
    )
    sources = decode_claim_sources(acquired.sources)
    encoding = acquired.sources.encoding
    price = PriceRevision.model_validate(
        {
            "id": binding.price_table_id,
            "namespace_id": acquired.price.namespace_id,
            "effective_from": acquired.price.effective_from.decode(encoding),
            "version": 1,
            "normalization_version": 1,
            "prices": _native_object(acquired.price.prices, encoding),
        }
    )
    raw = ObjectReader(checked.canonical_ir).object()
    nodes = cast(JsonObject, cast(JsonObject, raw["graph"])["nodes"])
    node = nodes.get(binding.node_path)
    if not isinstance(node, dict):
        raise ClaimSourcesRejected()
    inputs = read_attempt_inputs(
        sources.values["input_bindings"], cast(JsonObject, node.get("inputs", {}))
    )
    material = prepare_claim_material(
        checked, params, binding.node_path, inputs.claim_map(), price, selection
    )
    if acquired.policy is not None:
        policy_revision = acquired.policy.revision
        policy = prepare_claim_policy(
            _native_object(acquired.policy.payload, encoding),
            policy_revision,
            material.node_policy,
        )
    else:
        saved = sources.values["execution_policy"]
        if not isinstance(saved, dict) or set(saved) != {
            "namespace_revision",
            "values",
            "semantics",
        }:
            raise ClaimSourcesRejected()
        values = saved["values"]
        if (
            not isinstance(values, dict)
            or set(values) != {"namespace", "node"}
            or not isinstance(values["namespace"], dict)
            or type(saved["namespace_revision"]) is not int
        ):
            raise ClaimSourcesRejected()
        policy = prepare_claim_policy(
            values["namespace"], saved["namespace_revision"], material.node_policy
        )
        if (
            ObjectReader(policy.payload.decode("utf-8")).object()["execution_policy"]
            != saved
        ):
            raise ClaimSourcesRejected()
        policy_revision = None
    return PreparedClaimSnapshot(material, policy, sources, policy_revision)


def prepare_claim_snapshot(
    acquired: AcquiredClaimSnapshot,
    pin: GraphVersionPin,
    selection: RuntimeSelector,
    limits: ClaimSnapshotLimits,
) -> PreparedClaimSnapshot:
    try:
        return _prepare_claim_snapshot(acquired, pin, selection, limits)
    except (ValueError, KeyError, TypeError):
        del acquired, pin, selection, limits
        raise_safe(ClaimSourcesRejected())
