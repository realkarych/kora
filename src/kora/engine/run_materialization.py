from datetime import datetime
from enum import StrEnum
from typing import NamedTuple, Never, Self

from pydantic import MISSING

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, TextBudget
from kora.db.identifiers import is_identifier
from kora.db.types import normalise_timestamp
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_types import read_document
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import COMPONENT
from kora.engine.start_capacity import Overlap, resolve_manual_overlap
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

_ISSUER = object()
PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.run_materialization.prepared",
        COMPONENT,
        Level.DEBUG,
        "initial run materialization prepared",
        {"outcome": FieldKind.STRING},
    )
)


class MaterializationFailure(StrEnum):
    BINDING = "binding"
    CAPACITY = "capacity"
    VIEW = "view"


class MaterializationRejected(ValueError):
    def __init__(self, reason: MaterializationFailure) -> None:
        super().__init__(reason.value)
        self.reason: MaterializationFailure = reason


class InitialNode(NamedTuple):
    path: str
    kind: str
    trigger_rule: str


class PreparedRunMaterialization:
    __slots__ = ("_binding", "nodes", "timeout", "manual_overlap")
    _binding: tuple[str, str, str, str]
    nodes: tuple[InitialNode, ...]
    timeout: str | None
    manual_overlap: Overlap

    def __new__(
        cls,
        issuer: object,
        binding: tuple[str, str, str, str],
        nodes: tuple[InitialNode, ...],
        timeout: str | None,
        manual_overlap: Overlap,
    ) -> Self:
        if issuer is not _ISSUER:
            del issuer, binding, nodes, timeout, manual_overlap
            raise TypeError("run materialization requires checked preparation")
        result = super().__new__(cls)
        object.__setattr__(result, "_binding", binding)
        object.__setattr__(result, "nodes", nodes)
        object.__setattr__(result, "timeout", timeout)
        object.__setattr__(result, "manual_overlap", manual_overlap)
        return result

    def matches(self, scope: GraphResourceScope, pin: GraphVersionPin) -> bool:
        if type(scope) is not GraphResourceScope or type(pin) is not GraphVersionPin:
            return False
        return (
            all(
                type(field) is str
                for field in (
                    scope.namespace_id,
                    scope.graph_id,
                    scope.version_id,
                    pin.graph_id,
                    pin.version_id,
                    pin.execution_contract_digest,
                )
            )
            and (scope.graph_id, scope.version_id) == (pin.graph_id, pin.version_id)
            and self._binding
            == (
                scope.namespace_id,
                scope.graph_id,
                scope.version_id,
                pin.execution_contract_digest,
            )
        )

    def __setattr__(self, name: str, value: object) -> Never:
        del self, value
        raise AttributeError("prepared run materialization is immutable")

    def __delattr__(self, name: str) -> Never:
        del self
        raise AttributeError("prepared run materialization is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("prepared run materialization requires explicit access")


def _prepare(
    checked: CheckedExecutionContract, max_ir_bytes: int, max_top_level_nodes: int
) -> PreparedRunMaterialization | MaterializationFailure:
    if type(checked) is not CheckedExecutionContract or not hasattr(
        checked, "canonical_ir"
    ):
        return MaterializationFailure.BINDING
    if type(max_top_level_nodes) is not int or max_top_level_nodes <= 0:
        return MaterializationFailure.CAPACITY
    try:
        document = read_document(checked.canonical_ir, TextBudget(max_ir_bytes))
    except GraphValueRejected as rejected:
        rejected.__traceback__ = None
        return (
            MaterializationFailure.CAPACITY
            if rejected.reason is GraphValueFailure.BUDGET
            else MaterializationFailure.VIEW
        )
    nodes = document.graph.nodes
    if len(nodes) > max_top_level_nodes:
        return MaterializationFailure.CAPACITY
    scope, pin = checked.scope, checked.pin
    limits = document.runtime.limits
    timeout = None if limits is MISSING or limits.timeout is MISSING else limits.timeout
    return PreparedRunMaterialization(
        _ISSUER,
        (
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            pin.execution_contract_digest,
        ),
        tuple(
            InitialNode(
                name,
                node.kind,
                "all_success" if node.trigger_rule is MISSING else node.trigger_rule,
            )
            for name, node in nodes.items()
        ),
        timeout,
        resolve_manual_overlap(document.graph.overlap),
    )


def prepare_run_materialization(
    checked: CheckedExecutionContract, *, max_ir_bytes: int, max_top_level_nodes: int
) -> PreparedRunMaterialization:
    result = _prepare(checked, max_ir_bytes, max_top_level_nodes)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            PREPARED,
            outcome=result.value
            if isinstance(result, MaterializationFailure)
            else "accepted",
        )
    if isinstance(result, PreparedRunMaterialization):
        return result
    del checked, max_ir_bytes, max_top_level_nodes, diagnostics
    raise_safe(MaterializationRejected(result))


def _valid_initial_identities(
    scope: GraphResourceScope, run_id: str, node_ids: tuple[str, ...], count: int
) -> bool:
    if type(node_ids) is not tuple or len(node_ids) != count:
        return False
    reserved = {scope.namespace_id, scope.graph_id, scope.version_id}
    if type(run_id) is not str or not is_identifier(run_id) or run_id in reserved:
        return False
    reserved.add(run_id)
    for node_id in node_ids:
        if (
            type(node_id) is not str
            or not is_identifier(node_id)
            or node_id in reserved
        ):
            return False
        reserved.add(node_id)
    return True


def prepare_initial_node_rows(
    materialization: PreparedRunMaterialization,
    scope: GraphResourceScope,
    pin: GraphVersionPin,
    run_id: str,
    node_ids: tuple[str, ...],
    at: datetime,
) -> list[dict[str, object]]:
    stamp = None
    if (
        type(materialization) is PreparedRunMaterialization
        and hasattr(materialization, "nodes")
        and materialization.matches(scope, pin)
        and _valid_initial_identities(
            scope, run_id, node_ids, len(materialization.nodes)
        )
        and type(at) is datetime
    ):
        try:
            stamp = normalise_timestamp(at)
        except (TypeError, ValueError, OverflowError):
            pass
    if stamp is None or stamp.year < 1000:
        del materialization, scope, pin, run_id, node_ids, at, stamp
        raise_safe(MaterializationRejected(MaterializationFailure.BINDING))
    return [
        {
            "id": node_id,
            "run_id": run_id,
            "node_path": node.path,
            "kind": node.kind,
            "status": "pending",
            "trigger_rule": node.trigger_rule,
            "subgraph_attempt": 1,
            "top_level": True,
            "consumed_versions": {},
            "current_attempt_id": None,
            "branch_selection": None,
            "attempt_count": 0,
            "started_at": None,
            "deadline_expired_at": None,
            "provider_wait_deadline": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        for node, node_id in zip(materialization.nodes, node_ids, strict=True)
    ]
