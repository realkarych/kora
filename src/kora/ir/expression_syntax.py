from collections.abc import Iterable
from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.cel.diagnostics import DiagnosticError
from kora.cel.parser import parse
from kora.cel.paths import parse_path
from kora.ir.declarations import IrSourceLocation
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import from_cel
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import (
    FieldPath,
    SyntaxField,
    graph_fields,
    node_fields,
    retry_fields,
)
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.topology import Subgraph
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.expression_syntax.completed",
        COMPONENT,
        Level.INFO,
        "expression and path syntax validation completed",
        {
            "syntax_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "triggers": FieldKind.INTEGER,
            "expressions": FieldKind.INTEGER,
            "paths": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class ExpressionSyntaxResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    triggers_checked: int
    expressions_checked: int
    paths_checked: int

    @property
    def syntax_valid(self) -> bool:
        return self.diagnostics.valid


class SyntaxChecks:
    def __init__(self) -> None:
        self.collector = DiagnosticCollector()
        self.expressions: int = 0
        self.paths: int = 0

    def check(
        self,
        fields: Iterable[SyntaxField],
        prefix: FieldPath,
        source: IrSourceLocation | MISSING = MISSING,
        node: str | None = None,
    ) -> None:
        for field in fields:
            try:
                if field.is_path:
                    self.paths += 1
                    parse_path(field.text)
                else:
                    self.expressions += 1
                    parse(field.text)
            except DiagnosticError as error:
                self.collector.add(
                    from_cel(
                        error.diagnostic,
                        path=prefix + field.path,
                        exact_expression_field=True,
                        location=safe_source(source),
                        node=node,
                    )
                )


def validate_expression_syntax(document: GraphDocument) -> ExpressionSyntaxResult:
    checks = SyntaxChecks()
    graph = document.graph
    checks.check(
        retry_fields(
            document.runtime.infrastructure_retry,
            ("runtime", "infrastructure_retry"),
        ),
        (),
    )
    checks.check(graph_fields(graph), ("graph",), graph.source)
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = 0
    for body, path in bodies:
        if isinstance(body, Subgraph):
            checks.check(retry_fields(body.retry), path, body.source)
        for name, node in body.nodes.items():
            nodes += 1
            checks.check(node_fields(node), (*path, "nodes", name), node.source, name)
    result = ExpressionSyntaxResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        0 if graph.triggers is MISSING else len(graph.triggers),
        checks.expressions,
        checks.paths,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            syntax_valid=result.syntax_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            triggers=result.triggers_checked,
            expressions=result.expressions_checked,
            paths=result.paths_checked,
        )
    return result
