from dataclasses import dataclass

from pydantic import MISSING

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueRejected, TextBudget
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_types import read_document
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import COMPONENT
from kora.ir.first_slice_configuration import KINDS, validate_first_slice_configuration
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.readiness.prepared",
        COMPONENT,
        Level.DEBUG,
        "sequential readiness view prepared",
        {"prepared": FieldKind.BOOLEAN},
    )
)


class ReadinessRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("sequential readiness view is unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessInput:
    name: str
    producer: str
    output: str | None
    type_name: str
    path: str | None
    optional: bool


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessNode:
    path: str
    kind: str
    trigger_rule: str
    predecessors: tuple[str, ...]
    inputs: tuple[ReadinessInput, ...]

    def declared_inputs(self) -> JsonObject:
        return {
            entry.name: {
                "from": entry.producer,
                "output": entry.output,
                "type": entry.type_name,
                "path": entry.path,
                "optional": entry.optional,
            }
            for entry in self.inputs
        }


@dataclass(frozen=True, slots=True, repr=False)
class PreparedReadiness:
    binding: tuple[str, str, str, str]
    nodes: tuple[ReadinessNode, ...]

    def matches(self, scope: GraphResourceScope, pin: GraphVersionPin) -> bool:
        return (scope.graph_id, scope.version_id) == (
            pin.graph_id,
            pin.version_id,
        ) and (
            self.binding
            == (
                scope.namespace_id,
                scope.graph_id,
                scope.version_id,
                pin.execution_contract_digest,
            )
        )


def _prepare(checked: CheckedExecutionContract, maximum: int) -> PreparedReadiness:
    document = read_document(checked.canonical_ir, TextBudget(maximum))
    if not validate_first_slice_configuration(
        document
    ).authored_configuration_supported:
        raise ReadinessRejected()
    nodes = document.graph.nodes
    by_kind = {node.kind: name for name, node in nodes.items()}
    ordered = tuple(by_kind[kind] for kind in KINDS)
    prepared: list[ReadinessNode] = []
    for name in ordered:
        node = nodes[name]
        inputs: list[ReadinessInput] = []
        predecessors = set(() if node.depends_on is MISSING else node.depends_on)
        for input_name, source in () if node.inputs is MISSING else node.inputs.items():
            if source.from_ is MISSING or (
                source.from_ != "$params"
                and (source.from_ not in nodes or source.output is MISSING)
            ):
                raise ReadinessRejected()
            inputs.append(
                ReadinessInput(
                    input_name,
                    source.from_,
                    None if source.output is MISSING else source.output,
                    source.type,
                    None if source.path is MISSING else source.path,
                    source.optional is True,
                )
            )
            if source.from_ != "$params":
                predecessors.add(source.from_)
        prepared.append(
            ReadinessNode(
                name,
                node.kind,
                "all_success" if node.trigger_rule is MISSING else node.trigger_rule,
                tuple(value for value in ordered if value in predecessors),
                tuple(inputs),
            )
        )
    scope, pin = checked.scope, checked.pin
    return PreparedReadiness(
        (
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            pin.execution_contract_digest,
        ),
        tuple(prepared),
    )


def prepare_readiness(
    checked: CheckedExecutionContract, *, max_ir_bytes: int
) -> PreparedReadiness:
    try:
        prepared = _prepare(checked, max_ir_bytes)
    except (GraphValueRejected, ReadinessRejected):
        del checked, max_ir_bytes
        raise_safe(ReadinessRejected())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, prepared=True)
    return prepared
