from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, safe_source
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.root_previous_attempts.completed",
        COMPONENT,
        Level.INFO,
        "root previous-attempt input validation completed",
        {
            "root_previous_attempt_refs_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "inputs": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class RootPreviousAttemptResult:
    diagnostics: DiagnosticCollection
    root_nodes_checked: int
    root_inputs_checked: int

    @property
    def root_previous_attempt_refs_valid(self) -> bool:
        return self.diagnostics.valid


def validate_root_previous_attempt_refs(
    document: GraphDocument,
) -> RootPreviousAttemptResult:
    collector = DiagnosticCollector()
    inputs = 0
    for name, node in document.graph.nodes.items():
        if node.inputs is MISSING:
            continue
        for input_name, reference in node.inputs.items():
            inputs += 1
            if reference.from_ == "$previous_attempt":
                collector.add(
                    Diagnostic(
                        code="IR006",
                        message=(
                            "Previous-attempt input is unavailable in the root graph."
                        ),
                        path=("graph", "nodes", name, "inputs", input_name, "from"),
                        node=name,
                        location=safe_source(node.source),
                    )
                )
    result = RootPreviousAttemptResult(
        collector.finish(), len(document.graph.nodes), inputs
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            root_previous_attempt_refs_valid=result.root_previous_attempt_refs_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=result.root_nodes_checked,
            inputs=result.root_inputs_checked,
        )
    return result
