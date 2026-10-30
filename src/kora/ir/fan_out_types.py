from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.ir.declarations import OutputDecl
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, outputs, safe_source
from kora.ir.topology import FanOutNode, Subgraph
from kora.ir.type_ancestry import TypeAncestry, index_type_ancestry
from kora.ir.type_declarations import parse_type_shape
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.fan_out_types.completed",
        COMPONENT,
        Level.INFO,
        "fan-out type validation completed",
        {
            "fan_out_types_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "fan_outs": FieldKind.INTEGER,
            "collections": FieldKind.INTEGER,
            "contracts": FieldKind.INTEGER,
            "types_indexed": FieldKind.INTEGER,
            "mapped_inputs_checked": FieldKind.INTEGER,
            "mapped_inputs_unresolved": FieldKind.INTEGER,
            "collections_compared": FieldKind.INTEGER,
            "collections_unresolved": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class FanOutTypeResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    fan_outs_checked: int
    collections_checked: int
    contracts_checked: int
    types_indexed: int
    mapped_inputs_checked: int
    mapped_inputs_unresolved: int
    collections_compared: int
    collections_unresolved: int

    @property
    def fan_out_types_valid(self) -> bool:
        return self.diagnostics.valid


class FanOutTypeChecks:
    def __init__(self, ancestry: TypeAncestry) -> None:
        self.ancestry = ancestry
        self.collector = DiagnosticCollector()
        self.mapped_inputs_checked: int = 0
        self.mapped_inputs_unresolved: int = 0
        self.collections: int = 0
        self.compared: int = 0
        self.unresolved: int = 0

    def mapped_input(
        self, node: FanOutNode, name: str, path: tuple[str | int, ...]
    ) -> None:
        value = None if node.inputs is MISSING else node.inputs.get(node.config.over)
        shape = None if value is None else parse_type_shape(value.type)
        if shape is None or shape.base not in self.ancestry.intervals:
            self.mapped_inputs_unresolved += 1
            return
        self.mapped_inputs_checked += 1
        if shape.list_depth == 0:
            self.collector.add(
                Diagnostic(
                    code="IR004",
                    message="The mapped input type is not a list.",
                    path=path + ("config", "over"),
                    node=name,
                    location=safe_source(node.source),
                )
            )

    def collection(
        self,
        node: FanOutNode,
        name: str,
        body: Subgraph | None,
        value: OutputDecl,
        path: tuple[str | int, ...],
    ) -> None:
        self.collections += 1
        if body is None or body.outputs is MISSING or value.collect is MISSING:
            self.unresolved += 1
            return
        declaration = body.outputs.get(value.collect)
        child = None if declaration is None else parse_type_shape(declaration.type)
        ancestor = parse_type_shape(value.type)
        if child is None or ancestor is None:
            self.unresolved += 1
            return
        compatible = self.ancestry.accepts(child.base, ancestor.base)
        if compatible is None:
            self.unresolved += 1
            return
        self.compared += 1
        if not compatible or child.list_depth + 1 != ancestor.list_depth:
            self.collector.add(
                Diagnostic(
                    code="IR003",
                    message=(
                        "The collected export type is not compatible "
                        "with the output type."
                    ),
                    path=path,
                    node=name,
                    location=safe_source(node.source),
                )
            )


def validate_fan_out_types(document: GraphDocument) -> FanOutTypeResult:
    ancestry = index_type_ancestry(document.type_contracts)
    checks = FanOutTypeChecks(ancestry)
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = 0
    fan_outs = 0
    for scope, path in bodies:
        for name, node in scope.nodes.items():
            nodes += 1
            if not isinstance(node, FanOutNode):
                continue
            fan_outs += 1
            node_path = path + ("nodes", name)
            checks.mapped_input(node, name, node_path)
            body = definitions.get(node.config.body)
            for output_name, value in outputs(node).items():
                checks.collection(
                    node,
                    name,
                    body,
                    value,
                    node_path + ("outputs", output_name, "type"),
                )
    result = FanOutTypeResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        fan_outs,
        checks.collections,
        len(document.type_contracts),
        len(ancestry.intervals),
        checks.mapped_inputs_checked,
        checks.mapped_inputs_unresolved,
        checks.compared,
        checks.unresolved,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            fan_out_types_valid=result.fan_out_types_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=result.nodes_checked,
            definitions=result.definitions_checked,
            fan_outs=result.fan_outs_checked,
            collections=result.collections_checked,
            contracts=result.contracts_checked,
            types_indexed=result.types_indexed,
            mapped_inputs_checked=result.mapped_inputs_checked,
            mapped_inputs_unresolved=result.mapped_inputs_unresolved,
            collections_compared=result.collections_compared,
            collections_unresolved=result.collections_unresolved,
        )
    return result
