from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.ir.declarations import InputRef
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, outputs, safe_source
from kora.ir.topology import GraphBody, Node
from kora.ir.type_ancestry import TypeAncestry, index_type_ancestry
from kora.ir.type_declarations import parse_type_shape
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.edge_types.completed",
        COMPONENT,
        Level.INFO,
        "direct edge type validation completed",
        {
            "edge_types_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "inputs": FieldKind.INTEGER,
            "contracts": FieldKind.INTEGER,
            "types_indexed": FieldKind.INTEGER,
            "edges": FieldKind.INTEGER,
            "unresolved": FieldKind.INTEGER,
            "excluded": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class EdgeTypeResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    inputs_checked: int
    contracts_checked: int
    types_indexed: int
    edges_checked: int
    edges_unresolved: int
    edges_excluded: int

    @property
    def edge_types_valid(self) -> bool:
        return self.diagnostics.valid


class EdgeTypeChecks:
    def __init__(self, ancestry: TypeAncestry) -> None:
        self.ancestry = ancestry
        self.collector = DiagnosticCollector()
        self.inputs: int = 0
        self.checked: int = 0
        self.unresolved: int = 0
        self.excluded: int = 0

    def check(
        self,
        body: GraphBody,
        node: Node,
        name: str,
        value: InputRef,
        path: tuple[str | int, ...],
    ) -> None:
        self.inputs += 1
        if value.path is not MISSING or value.from_ in (
            "$params",
            "$inputs",
            "$previous_attempt",
        ):
            self.excluded += 1
            return
        if value.from_ is MISSING or value.output is MISSING:
            self.unresolved += 1
            return
        producer = body.nodes.get(value.from_)
        declaration = None if producer is None else outputs(producer).get(value.output)
        if declaration is None:
            self.unresolved += 1
            return
        produced = parse_type_shape(declaration.type)
        expected = parse_type_shape(value.type)
        if produced is None or expected is None:
            self.unresolved += 1
            return
        compatible = self.ancestry.accepts(produced.base, expected.base)
        if compatible is None:
            self.unresolved += 1
            return
        self.checked += 1
        if not compatible or produced.list_depth != expected.list_depth:
            self.collector.add(
                Diagnostic(
                    code="IR003",
                    message="The producer type is not compatible with the input type.",
                    path=path,
                    node=name,
                    location=safe_source(node.source),
                )
            )


def validate_edge_types(document: GraphDocument) -> EdgeTypeResult:
    ancestry = index_type_ancestry(document.type_contracts)
    checks = EdgeTypeChecks(ancestry)
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = 0
    for body, path in bodies:
        for name, node in body.nodes.items():
            nodes += 1
            if node.inputs is MISSING:
                continue
            for input_name, value in node.inputs.items():
                checks.check(
                    body,
                    node,
                    name,
                    value,
                    path + ("nodes", name, "inputs", input_name, "type"),
                )
    result = EdgeTypeResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.inputs,
        len(document.type_contracts),
        len(ancestry.intervals),
        checks.checked,
        checks.unresolved,
        checks.excluded,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            edge_types_valid=result.edge_types_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            inputs=result.inputs_checked,
            contracts=result.contracts_checked,
            types_indexed=result.types_indexed,
            edges=result.edges_checked,
            unresolved=result.edges_unresolved,
            excluded=result.edges_excluded,
        )
    return result
