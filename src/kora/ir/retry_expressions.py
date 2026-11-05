from dataclasses import dataclass
from enum import StrEnum
from itertools import chain

from pydantic import MISSING

from kora.cel import CheckedExpression, Environment, ResultKind, Variable, check_typed
from kora.cel.diagnostics import Diagnostic as CelDiagnostic
from kora.cel.diagnostics import DiagnosticError
from kora.ir.approval_inputs import InputFailure
from kora.ir.cel_inventory_admission import (
    CheckedInventoryAdmission,
    inventory_meter,
    retain_checked_site,
)
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import from_cel
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts, required_context
from kora.ir.expression_fields import FieldPath, retry_fields
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.topology import GraphBody, Node, Subgraph
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.retry_expressions.completed",
        COMPONENT,
        Level.INFO,
        "partial ordinary retry expression validation completed",
        {
            "retry_expressions_complete": FieldKind.BOOLEAN,
            "retry_expressions_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "policies": FieldKind.INTEGER,
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
class RetryExpressionResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    policies_checked: int
    expressions_seen: int
    expressions_checked: int
    syntax_unresolved: int
    context_unresolved: int
    schema_unresolved: int
    schema_steps: int
    checker_steps: int

    @property
    def retry_expressions_complete(self) -> bool:
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
    def retry_expressions_valid(self) -> bool:
        return self.retry_expressions_complete and self.diagnostics.valid


@dataclass(frozen=True, slots=True, repr=False)
class CheckedRetrySite:
    path: FieldPath
    checked: CheckedExpression


class InfrastructureRetryPrerequisite(StrEnum):
    SYNTAX_UNRESOLVED = "syntax_unresolved"
    SCHEMA_UNRESOLVED = "schema_unresolved"


def checked_infrastructure_retry_site(
    document: GraphDocument,
    *,
    admission: CheckedInventoryAdmission | None = None,
) -> CheckedRetrySite | DiagnosticCollection | InfrastructureRetryPrerequisite | None:
    policy = document.runtime.infrastructure_retry
    if policy is MISSING or policy.predicate is MISSING:
        return None
    source = policy.predicate
    meter = inventory_meter(admission)
    try:
        required = required_context(source, meter, frozenset({"params"}))
    except DiagnosticError:
        return InfrastructureRetryPrerequisite.SYNTAX_UNRESOLVED
    variables: dict[str, Variable] = {}
    if "params" in required:
        contexts = InputParameterContexts(
            document.type_contracts, document.graph.params, admission
        )
        parameter = contexts.parameter_context()
        if parameter is None:
            return InfrastructureRetryPrerequisite.SCHEMA_UNRESOLVED
        variables["params"] = parameter
    checked = check_typed(source, Environment(variables, ResultKind.BOOL), meter)
    path: FieldPath = ("runtime", "infrastructure_retry", "predicate")
    if isinstance(checked, CheckedExpression):
        retain_checked_site(admission, path, checked)
        return CheckedRetrySite(path, checked)
    collector = DiagnosticCollector()
    collector.add(from_cel(checked, path=path, exact_expression_field=True))
    return collector.finish()


class RetryExpressionChecks(InputParameterContexts):
    def __init__(
        self,
        document: GraphDocument,
        admission: CheckedInventoryAdmission | None = None,
    ) -> None:
        super().__init__(document.type_contracts, document.graph.params, admission)
        self.collector: DiagnosticCollector = DiagnosticCollector()
        self.policies: int = 0
        self.seen: int = 0
        self.checked: int = 0
        self.syntax_unresolved: int = 0
        self.context_unresolved: int = 0
        self.schema_unresolved: int = 0

    def check(
        self,
        body: GraphBody,
        node: Node | Subgraph,
        name: str | None,
        path: FieldPath,
        sites: list[CheckedRetrySite] | None,
    ) -> None:
        self.policies += int(node.retry is not MISSING)
        for field in retry_fields(node.retry):
            self.seen += 1
            try:
                required = required_context(
                    field.text, self.meter, frozenset({"inputs", "outputs", "params"})
                )
                variables = self.retry_context(body, node, required)
            except DiagnosticError:
                self.syntax_unresolved += 1
                continue
            if variables is InputFailure.CONTEXT:
                self.context_unresolved += 1
                continue
            if variables is InputFailure.SCHEMA:
                self.schema_unresolved += 1
                continue
            self.checked += 1
            checked = check_typed(
                field.text,
                Environment(
                    variables, ResultKind.BOOL, frozenset({"inputs", "outputs"})
                ),
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
                sites.append(CheckedRetrySite(field_path, checked))

    def retry_context(
        self, body: GraphBody, node: Node | Subgraph, required: frozenset[str]
    ) -> dict[str, Variable] | InputFailure:
        if isinstance(node, Subgraph):
            if required & {"inputs", "outputs"}:
                return InputFailure.CONTEXT
            body_variables: dict[str, Variable] = {}
            if "params" in required:
                parameter = self.parameter_context()
                if parameter is None:
                    return InputFailure.SCHEMA
                body_variables["params"] = parameter
            return body_variables
        variables = self.variables(body, node, required)
        if isinstance(variables, InputFailure):
            return variables
        if "outputs" in required:
            outputs = self.inputs.output_context(node)
            if isinstance(outputs, InputFailure):
                return outputs
            variables["outputs"] = outputs
        return variables


def validate_retry_expressions(document: GraphDocument) -> RetryExpressionResult:
    return _check_retry_expressions(document, None)


def checked_retry_sites(
    document: GraphDocument,
    *,
    max_declarations: int,
    admission: CheckedInventoryAdmission | None = None,
) -> tuple[CheckedRetrySite, ...] | RetryExpressionResult | None:
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    remaining = max_declarations - 1 - len(document.type_contracts) - len(definitions)
    if remaining < 0:
        return None
    for body in chain((graph,), definitions.values()):
        remaining -= len(body.nodes)
        if remaining < 0:
            return None
    sites: list[CheckedRetrySite] = []
    result = _check_retry_expressions(document, sites, admission)
    if not result.retry_expressions_valid:
        return result
    return tuple(sites)


def _check_retry_expressions(
    document: GraphDocument,
    sites: list[CheckedRetrySite] | None,
    admission: CheckedInventoryAdmission | None = None,
) -> RetryExpressionResult:
    checks = RetryExpressionChecks(document, admission)
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = 0
    for body, path in bodies:
        if isinstance(body, Subgraph):
            checks.check(body, body, None, path, sites)
        for name, node in body.nodes.items():
            nodes += 1
            checks.check(body, node, name, (*path, "nodes", name), sites)
    result = RetryExpressionResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.policies,
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
            retry_expressions_complete=result.retry_expressions_complete,
            retry_expressions_valid=result.retry_expressions_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            policies=result.policies_checked,
            expressions_seen=result.expressions_seen,
            expressions_checked=result.expressions_checked,
            syntax_unresolved=result.syntax_unresolved,
            context_unresolved=result.context_unresolved,
            schema_unresolved=result.schema_unresolved,
            schema_steps=result.schema_steps,
            checker_steps=result.checker_steps,
        )
    return result
