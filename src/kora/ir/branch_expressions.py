from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.cel import CheckedExpression, Environment, ResultKind, check_typed
from kora.cel.diagnostics import Diagnostic as CelDiagnostic
from kora.cel.diagnostics import DiagnosticError
from kora.ir.approval_inputs import InputFailure
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import from_cel
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.expression_fields import FieldPath, branch_fields
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.topology import BranchNode, GraphBody
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.branch_expressions.completed",
        COMPONENT,
        Level.INFO,
        "partial branch expression validation completed",
        {
            "branch_expressions_complete": FieldKind.BOOLEAN,
            "branch_expressions_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "branches": FieldKind.INTEGER,
            "expressions_seen": FieldKind.INTEGER,
            "expressions_checked": FieldKind.INTEGER,
            "syntax_unresolved": FieldKind.INTEGER,
            "context_unresolved": FieldKind.INTEGER,
            "schema_unresolved": FieldKind.INTEGER,
            "schema_steps": FieldKind.INTEGER,
            "checker_steps": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class BranchExpressionResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    branches_checked: int
    expressions_seen: int
    expressions_checked: int
    syntax_unresolved: int
    context_unresolved: int
    schema_unresolved: int
    schema_steps: int
    checker_steps: int

    @property
    def branch_expressions_complete(self) -> bool:
        return not any(
            (
                self.syntax_unresolved,
                self.context_unresolved,
                self.schema_unresolved,
                self.diagnostics.errors_omitted,
                self.diagnostics.warnings_omitted,
            )
        )

    @property
    def branch_expressions_valid(self) -> bool:
        return self.branch_expressions_complete and self.diagnostics.valid


@dataclass(frozen=True, slots=True, repr=False)
class CheckedBranchSite:
    path: FieldPath
    checked: CheckedExpression


class BranchExpressionChecks(InputParameterContexts):
    def __init__(self, document: GraphDocument) -> None:
        super().__init__(document.type_contracts, document.graph.params)
        self.collector: DiagnosticCollector = DiagnosticCollector()
        self.branches: int = 0
        self.seen: int = 0
        self.checked: int = 0
        self.syntax_unresolved: int = 0
        self.context_unresolved: int = 0
        self.schema_unresolved: int = 0

    def check(
        self,
        body: GraphBody,
        node: BranchNode,
        name: str,
        path: FieldPath,
        sites: list[CheckedBranchSite] | None,
    ) -> None:
        self.branches += 1
        for field in branch_fields(node):
            self.seen += 1
            try:
                variables = self.context(body, node, field.text)
            except DiagnosticError:
                self.syntax_unresolved += 1
                continue
            if variables is InputFailure.CONTEXT:
                self.context_unresolved += 1
                continue
            if variables is InputFailure.SCHEMA:
                self.schema_unresolved += 1
                continue
            kind = ResultKind.BOOL if field.path[-1] == "when" else ResultKind.STRING
            self.checked += 1
            checked = check_typed(
                field.text,
                Environment(variables, kind, frozenset({"inputs"})),
                self.meter,
            )
            field_path = (*path, *field.path)
            if isinstance(checked, CelDiagnostic):
                self.collector.add(
                    from_cel(
                        checked,
                        path=field_path,
                        exact_expression_field=True,
                        node=name,
                        location=safe_source(node.source),
                    )
                )
            elif sites is not None:
                sites.append(CheckedBranchSite(field_path, checked))


def validate_branch_expressions(document: GraphDocument) -> BranchExpressionResult:
    return _check_branch_expressions(document, None)


def checked_branch_sites(
    document: GraphDocument, *, max_declarations: int
) -> tuple[CheckedBranchSite, ...] | BranchExpressionResult | None:
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    remaining = max_declarations - 1 - len(document.type_contracts) - len(definitions)
    if remaining < 0:
        return None
    for body in chain((graph,), definitions.values()):
        remaining -= len(body.nodes)
        if remaining < 0:
            return None
        for node in body.nodes.values():
            if isinstance(node, BranchNode):
                remaining -= len(node.config.cases)
                if remaining < 0:
                    return None
    sites: list[CheckedBranchSite] = []
    result = _check_branch_expressions(document, sites)
    if not result.branch_expressions_valid:
        return result
    return tuple(sites)


def _check_branch_expressions(
    document: GraphDocument, sites: list[CheckedBranchSite] | None
) -> BranchExpressionResult:
    checks = BranchExpressionChecks(document)
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
            if isinstance(node, BranchNode):
                checks.check(body, node, name, (*path, "nodes", name), sites)
    result = BranchExpressionResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.branches,
        checks.seen,
        checks.checked,
        checks.syntax_unresolved,
        checks.context_unresolved,
        checks.schema_unresolved,
        checks.schemas.meter.steps,
        checks.meter.steps,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            branch_expressions_complete=result.branch_expressions_complete,
            branch_expressions_valid=result.branch_expressions_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            branches=result.branches_checked,
            expressions_seen=result.expressions_seen,
            expressions_checked=result.expressions_checked,
            syntax_unresolved=result.syntax_unresolved,
            context_unresolved=result.context_unresolved,
            schema_unresolved=result.schema_unresolved,
            schema_steps=result.schema_steps,
            checker_steps=result.checker_steps,
        )
    return result
