from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.cel.diagnostics import DiagnosticError
from kora.cel.paths import Field, parse_path
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath, SyntaxField, input_path_fields
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.topology import Node
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.input_paths.completed",
        COMPONENT,
        Level.INFO,
        "node input path validation completed",
        {
            "input_paths_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "paths": FieldKind.INTEGER,
            "bindings": FieldKind.INTEGER,
            "unresolved": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class InputPathResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    paths_checked: int
    bindings_checked: int
    paths_unresolved: int

    @property
    def input_paths_valid(self) -> bool:
        return self.diagnostics.valid and self.paths_unresolved == 0


class InputPathChecks:
    def __init__(self) -> None:
        self.collector = DiagnosticCollector()
        self.paths: int = 0
        self.bindings: int = 0
        self.unresolved: int = 0

    def check(
        self, node: Node, name: str, field: SyntaxField, prefix: FieldPath
    ) -> None:
        self.paths += 1
        try:
            steps = parse_path(field.text).steps
        except DiagnosticError:
            self.unresolved += 1
            return
        self.bindings += 1
        if (
            len(steps) >= 2
            and steps[0] == Field("inputs")
            and isinstance(steps[1], Field)
            and node.inputs is not MISSING
            and steps[1].name in node.inputs
        ):
            return
        self.collector.add(
            Diagnostic(
                code="IR005" if field.path[:2] == ("config", "binds") else "IR002",
                message="The path must select a declared input of this node.",
                path=prefix + field.path,
                node=name,
                location=safe_source(node.source),
            )
        )


def validate_input_paths(document: GraphDocument) -> InputPathResult:
    checks = InputPathChecks()
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
            for field in input_path_fields(node):
                checks.check(node, name, field, (*path, "nodes", name))
    result = InputPathResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.paths,
        checks.bindings,
        checks.unresolved,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            input_paths_valid=result.input_paths_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            paths=result.paths_checked,
            bindings=result.bindings_checked,
            unresolved=result.paths_unresolved,
        )
    return result
