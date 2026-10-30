from collections.abc import Mapping
from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.ir.declarations import InputRef, OutputDecl, SubgraphOutput
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, outputs, safe_source
from kora.ir.topology import SubgraphNode
from kora.ir.type_ancestry import TypeAncestry, index_type_ancestry
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.subgraph_interfaces.completed",
        COMPONENT,
        Level.INFO,
        "ordinary subgraph interface validation completed",
        {
            "interfaces_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "wrappers": FieldKind.INTEGER,
            "wrappers_unresolved": FieldKind.INTEGER,
            "contracts": FieldKind.INTEGER,
            "types_indexed": FieldKind.INTEGER,
            "inputs": FieldKind.INTEGER,
            "outputs": FieldKind.INTEGER,
            "compared": FieldKind.INTEGER,
            "unresolved": FieldKind.INTEGER,
            "names_mismatched": FieldKind.INTEGER,
        },
    )
)

Declarations = Mapping[str, InputRef | OutputDecl | SubgraphOutput]


@dataclass(frozen=True)
class SubgraphInterfaceResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    wrappers_checked: int
    wrappers_unresolved: int
    contracts_checked: int
    types_indexed: int
    inputs_checked: int
    outputs_checked: int
    types_compared: int
    types_unresolved: int
    names_mismatched: int

    @property
    def interfaces_valid(self) -> bool:
        return self.diagnostics.valid


class SubgraphInterfaceChecks:
    def __init__(self, ancestry: TypeAncestry) -> None:
        self.ancestry = ancestry
        self.collector = DiagnosticCollector()
        self.compared: int = 0
        self.unresolved: int = 0
        self.names_mismatched: int = 0

    def error(
        self,
        node: SubgraphNode,
        name: str,
        path: tuple[str | int, ...],
        message: str,
    ) -> None:
        self.collector.add(
            Diagnostic(
                code="IR003",
                message=message,
                path=path,
                node=name,
                location=safe_source(node.source),
            )
        )

    def interface(
        self,
        node: SubgraphNode,
        name: str,
        path: tuple[str | int, ...],
        actual: Declarations,
        expected: Declarations,
        *,
        preserve_outputs: bool,
    ) -> int:
        checked = len(actual)
        for member, value in actual.items():
            declaration = expected.get(member)
            if declaration is None:
                if preserve_outputs:
                    self.unresolved += 1
                else:
                    self.names_mismatched += 1
                    self.error(
                        node,
                        name,
                        path + (member,),
                        "The wrapper declares a name absent from the body interface.",
                    )
                continue
            compatible = (
                self.ancestry.accepts_type(declaration.type, value.type)
                if preserve_outputs
                else self.ancestry.accepts_type(value.type, declaration.type)
            )
            if compatible is None:
                self.unresolved += 1
                continue
            self.compared += 1
            if preserve_outputs:
                compatible = value.type == declaration.type
            if not compatible:
                self.error(
                    node,
                    name,
                    path + (member, "type"),
                    "The wrapper output type must match the declared body export type."
                    if preserve_outputs
                    else (
                        "The wrapper input type is not compatible "
                        "with the body input type."
                    ),
                )
        for member in expected:
            if member not in actual:
                checked += 1
                self.names_mismatched += 1
                self.error(
                    node,
                    name,
                    path + (member,),
                    "The wrapper omits a name declared by the body interface.",
                )
        return checked


def validate_subgraph_interfaces(document: GraphDocument) -> SubgraphInterfaceResult:
    ancestry = index_type_ancestry(document.type_contracts)
    checks = SubgraphInterfaceChecks(ancestry)
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = wrappers = missing_bodies = inputs = exported = 0
    for body, path in bodies:
        for name, node in body.nodes.items():
            nodes += 1
            if not isinstance(node, SubgraphNode):
                continue
            wrappers += 1
            definition = definitions.get(node.config.body)
            if definition is None:
                missing_bodies += 1
                continue
            node_path = path + ("nodes", name)
            inputs += checks.interface(
                node,
                name,
                node_path + ("inputs",),
                {} if node.inputs is MISSING else node.inputs,
                {} if definition.inputs is MISSING else definition.inputs,
                preserve_outputs=False,
            )
            exported += checks.interface(
                node,
                name,
                node_path + ("outputs",),
                outputs(node),
                {} if definition.outputs is MISSING else definition.outputs,
                preserve_outputs=True,
            )
    result = SubgraphInterfaceResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        wrappers,
        missing_bodies,
        len(document.type_contracts),
        len(ancestry.intervals),
        inputs,
        exported,
        checks.compared,
        checks.unresolved,
        checks.names_mismatched,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            interfaces_valid=result.interfaces_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            wrappers=wrappers,
            wrappers_unresolved=missing_bodies,
            contracts=result.contracts_checked,
            types_indexed=result.types_indexed,
            inputs=inputs,
            outputs=exported,
            compared=result.types_compared,
            unresolved=result.types_unresolved,
            names_mismatched=result.names_mismatched,
        )
    return result
