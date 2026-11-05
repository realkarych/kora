from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.cel import CheckedExpression, Environment, ResultKind, Variable, check_typed
from kora.cel.diagnostics import Diagnostic as CelDiagnostic
from kora.cel.diagnostics import DiagnosticError
from kora.cel.types import DYN
from kora.ir.approval_inputs import InputFailure
from kora.ir.cel_inventory_admission import (
    CheckedInventoryAdmission,
    retain_checked_site,
)
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import from_cel
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.expression_fields import FieldPath, wait_fields
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.topology import GraphBody, WaitEventNode
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.wait_expressions.completed",
        COMPONENT,
        Level.INFO,
        "partial wait expression validation completed",
        {
            "wait_expressions_complete": FieldKind.BOOLEAN,
            "wait_expressions_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "waits": FieldKind.INTEGER,
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
class WaitExpressionResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    waits_checked: int
    expressions_seen: int
    expressions_checked: int
    syntax_unresolved: int
    context_unresolved: int
    schema_unresolved: int
    schema_steps: int
    checker_steps: int

    @property
    def wait_expressions_complete(self) -> bool:
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
    def wait_expressions_valid(self) -> bool:
        return self.wait_expressions_complete and self.diagnostics.valid


@dataclass(frozen=True, slots=True, repr=False)
class CheckedWaitSite:
    path: FieldPath
    checked: CheckedExpression


class WaitExpressionChecks(InputParameterContexts):
    def __init__(
        self,
        document: GraphDocument,
        admission: CheckedInventoryAdmission | None = None,
    ) -> None:
        super().__init__(document.type_contracts, document.graph.params, admission)
        self.collector: DiagnosticCollector = DiagnosticCollector()
        self.waits: int = 0
        self.seen: int = 0
        self.checked: int = 0
        self.syntax_unresolved: int = 0
        self.context_unresolved: int = 0
        self.schema_unresolved: int = 0

    def check(
        self,
        body: GraphBody,
        node: WaitEventNode,
        name: str,
        path: FieldPath,
        sites: list[CheckedWaitSite] | None,
    ) -> None:
        self.waits += 1
        for field in wait_fields(node):
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
            external = "payload" if field.path[-1] == "match" else "observation"
            variables[external] = Variable(DYN)
            self.checked += 1
            checked = check_typed(
                field.text,
                Environment(variables, ResultKind.BOOL, frozenset({"inputs"})),
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
                retain_checked_site(self.admission, field_path, checked)
                sites.append(CheckedWaitSite(field_path, checked))


def validate_wait_expressions(document: GraphDocument) -> WaitExpressionResult:
    return _check_wait_expressions(document, None)


def checked_wait_sites(
    document: GraphDocument,
    *,
    max_declarations: int,
    admission: CheckedInventoryAdmission | None = None,
) -> tuple[CheckedWaitSite, ...] | WaitExpressionResult | None:
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    remaining = max_declarations - 1 - len(document.type_contracts) - len(definitions)
    if remaining < 0:
        return None
    for body in chain((graph,), definitions.values()):
        remaining -= len(body.nodes)
        if remaining < 0:
            return None
    sites: list[CheckedWaitSite] = []
    result = _check_wait_expressions(document, sites, admission)
    if not result.wait_expressions_valid:
        return result
    return tuple(sites)


def _check_wait_expressions(
    document: GraphDocument,
    sites: list[CheckedWaitSite] | None,
    admission: CheckedInventoryAdmission | None = None,
) -> WaitExpressionResult:
    checks = WaitExpressionChecks(document, admission)
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
            if isinstance(node, WaitEventNode):
                checks.check(body, node, name, (*path, "nodes", name), sites)
    result = WaitExpressionResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.waits,
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
            wait_expressions_complete=result.wait_expressions_complete,
            wait_expressions_valid=result.wait_expressions_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            waits=result.waits_checked,
            expressions_seen=result.expressions_seen,
            expressions_checked=result.expressions_checked,
            syntax_unresolved=result.syntax_unresolved,
            context_unresolved=result.context_unresolved,
            schema_unresolved=result.schema_unresolved,
            schema_steps=result.schema_steps,
            checker_steps=result.checker_steps,
        )
    return result
