from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.cel import (
    CheckedExpression,
    Environment,
    ResultKind,
    SchemaError,
    check_typed,
)
from kora.cel.diagnostics import Diagnostic as CelDiagnostic
from kora.cel.diagnostics import DiagnosticError
from kora.ir.approval_inputs import InputFailure
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission
from kora.ir.contracts import TypeContract
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import from_cel
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.json import JsonObject
from kora.ir.topology import GraphBody, HumanInputNode
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.approval_predicates.completed",
        COMPONENT,
        Level.INFO,
        "partial approval predicate validation completed",
        {
            "approval_predicates_complete": FieldKind.BOOLEAN,
            "approval_predicates_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "human_inputs": FieldKind.INTEGER,
            "predicates_seen": FieldKind.INTEGER,
            "predicates_checked": FieldKind.INTEGER,
            "syntax_unresolved": FieldKind.INTEGER,
            "context_unresolved": FieldKind.INTEGER,
            "schema_unresolved": FieldKind.INTEGER,
            "schema_steps": FieldKind.INTEGER,
            "checker_steps": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class ApprovalResponsePredicateResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    human_inputs_checked: int
    predicates_seen: int
    predicates_checked: int
    syntax_unresolved: int
    context_unresolved: int
    schema_unresolved: int
    schema_steps: int
    checker_steps: int

    @property
    def approval_predicates_complete(self) -> bool:
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
    def approval_predicates_valid(self) -> bool:
        return self.approval_predicates_complete and self.diagnostics.valid


class ApprovalPredicateChecks(InputParameterContexts):
    def __init__(
        self,
        contracts: dict[str, TypeContract],
        parameters: JsonObject | MISSING,
        collector: DiagnosticCollector | None = None,
        admission: CheckedInventoryAdmission | None = None,
    ) -> None:
        super().__init__(contracts, parameters, admission)
        self.collector: DiagnosticCollector = (
            DiagnosticCollector() if collector is None else collector
        )
        self.human_inputs: int = 0
        self.seen: int = 0
        self.checked: int = 0
        self.syntax_unresolved: int = 0
        self.context_unresolved: int = 0
        self.schema_unresolved: int = 0

    def check(
        self,
        body: GraphBody,
        node: HumanInputNode,
        name: str,
        path: tuple[str | int, ...],
    ) -> CheckedExpression | None:
        self.human_inputs += 1
        predicate = node.config.approve_when
        if predicate is MISSING:
            return None
        self.seen += 1
        try:
            variables = self.context(body, node, predicate)
        except DiagnosticError:
            self.syntax_unresolved += 1
            return None
        if variables is InputFailure.CONTEXT:
            self.context_unresolved += 1
            return None
        if variables is InputFailure.SCHEMA:
            self.schema_unresolved += 1
            return None
        form = self.default_form if node.config.form is MISSING else node.config.form
        try:
            variables["response"] = self.schemas.variable(form)
        except SchemaError:
            self.schema_unresolved += 1
            return None
        self.checked += 1
        checked = check_typed(
            predicate,
            Environment(variables, ResultKind.BOOL, frozenset({"inputs"})),
            self.meter,
        )
        if isinstance(checked, CelDiagnostic):
            self.collector.add(
                from_cel(
                    checked,
                    path=(*path, "config", "approve_when"),
                    exact_expression_field=True,
                    node=name,
                    location=safe_source(node.source),
                )
            )
            return None
        return checked


def validate_approval_response_predicates(
    document: GraphDocument,
) -> ApprovalResponsePredicateResult:
    checks = ApprovalPredicateChecks(document.type_contracts, document.graph.params)
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
            if isinstance(node, HumanInputNode):
                checks.check(body, node, name, (*path, "nodes", name))
    result = ApprovalResponsePredicateResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.human_inputs,
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
            approval_predicates_complete=result.approval_predicates_complete,
            approval_predicates_valid=result.approval_predicates_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            human_inputs=result.human_inputs_checked,
            predicates_seen=result.predicates_seen,
            predicates_checked=result.predicates_checked,
            syntax_unresolved=result.syntax_unresolved,
            context_unresolved=result.context_unresolved,
            schema_unresolved=result.schema_unresolved,
            schema_steps=result.schema_steps,
            checker_steps=result.checker_steps,
        )
    return result
