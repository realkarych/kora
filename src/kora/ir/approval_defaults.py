from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.cel import Environment, ResultKind, SchemaError, SchemaTypes, check_typed
from kora.cel.diagnostics import Diagnostic as CelDiagnostic
from kora.cel.meter import Meter
from kora.cel.types import Record, unwrap
from kora.ir.approval_form import default_approval_form
from kora.ir.contracts import TypeContract
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.schema_catalogue import contract_schema
from kora.ir.topology import HumanInputNode
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.approval_defaults.completed",
        COMPONENT,
        Level.INFO,
        "omitted approval predicate validation completed",
        {
            "approval_defaults_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "human_inputs": FieldKind.INTEGER,
            "predicates": FieldKind.INTEGER,
            "unresolved": FieldKind.INTEGER,
            "expressions": FieldKind.INTEGER,
            "schema_steps": FieldKind.INTEGER,
            "checker_steps": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class ApprovalDefaultResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    human_inputs_checked: int
    predicates_checked: int
    predicates_unresolved: int
    expressions_checked: int
    schema_steps: int
    checker_steps: int

    @property
    def approval_defaults_valid(self) -> bool:
        return self.diagnostics.valid and self.predicates_unresolved == 0


class ApprovalDefaultChecks:
    def __init__(self, contracts: dict[str, TypeContract]) -> None:
        roots = {
            "urn:kora:artifact:" + name.replace("/", ":"): root
            for name, contract in contracts.items()
            if (root := contract_schema(name, contract)) is not None
        }
        self.schemas = SchemaTypes(roots.get)
        self.checker_meter = Meter()
        self.collector = DiagnosticCollector()
        self.default_form = default_approval_form()
        self.human_inputs: int = 0
        self.predicates: int = 0
        self.unresolved: int = 0
        self.expressions: int = 0

    def check(
        self, node: HumanInputNode, name: str, path: tuple[str | int, ...]
    ) -> None:
        self.human_inputs += 1
        if node.config.approve_when is not MISSING:
            return
        form = self.default_form if node.config.form is MISSING else node.config.form
        try:
            response = self.schemas.variable(form)
        except SchemaError:
            self.unresolved += 1
            return
        self.predicates += 1
        record = unwrap(response.type)
        if isinstance(record, Record) and "decision" in record.fields:
            self.expressions += 1
            checked = check_typed(
                "response.decision == 'approve'",
                Environment({"response": response}, ResultKind.BOOL),
                self.checker_meter,
            )
            if not isinstance(checked, CelDiagnostic):
                return
        self.collector.add(
            Diagnostic(
                code="IR011",
                message="The form requires an explicit approval predicate.",
                path=(*path, "config", "approve_when"),
                node=name,
                location=safe_source(node.source),
            )
        )


def validate_approval_defaults(document: GraphDocument) -> ApprovalDefaultResult:
    checks = ApprovalDefaultChecks(document.type_contracts)
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
                checks.check(node, name, (*path, "nodes", name))
    result = ApprovalDefaultResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        checks.human_inputs,
        checks.predicates,
        checks.unresolved,
        checks.expressions,
        checks.schemas.meter.steps,
        checks.checker_meter.steps,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            approval_defaults_valid=result.approval_defaults_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            human_inputs=result.human_inputs_checked,
            predicates=result.predicates_checked,
            unresolved=result.predicates_unresolved,
            expressions=result.expressions_checked,
            schema_steps=result.schema_steps,
            checker_steps=result.checker_steps,
        )
    return result
