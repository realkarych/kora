from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.declarations import SubgraphOutput
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, outputs, safe_source
from kora.ir.topology import Subgraph
from kora.ir.type_ancestry import TypeAncestry, index_type_ancestry
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.export_types.completed",
        COMPONENT,
        Level.INFO,
        "subgraph export type validation completed",
        {
            "export_types_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "exports": FieldKind.INTEGER,
            "contracts": FieldKind.INTEGER,
            "types_indexed": FieldKind.INTEGER,
            "compared": FieldKind.INTEGER,
            "unresolved": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class ExportTypeResult:
    diagnostics: DiagnosticCollection
    definitions_checked: int
    exports_checked: int
    contracts_checked: int
    types_indexed: int
    exports_compared: int
    exports_unresolved: int

    @property
    def export_types_valid(self) -> bool:
        return self.diagnostics.valid


class ExportTypeChecks:
    def __init__(self, ancestry: TypeAncestry) -> None:
        self.ancestry = ancestry
        self.collector = DiagnosticCollector()
        self.exports: int = 0
        self.compared: int = 0
        self.unresolved: int = 0

    def check(
        self,
        body: Subgraph,
        value: SubgraphOutput,
        path: tuple[str | int, ...],
    ) -> None:
        self.exports += 1
        producer = body.nodes.get(value.from_)
        declaration = None if producer is None else outputs(producer).get(value.output)
        if declaration is None:
            self.unresolved += 1
            return
        compatible = self.ancestry.accepts_type(declaration.type, value.type)
        if compatible is None:
            self.unresolved += 1
            return
        self.compared += 1
        if not compatible:
            self.collector.add(
                Diagnostic(
                    code="IR003",
                    message="The producer type is not compatible with the export type.",
                    path=path,
                    location=safe_source(body.source),
                )
            )


def validate_export_types(document: GraphDocument) -> ExportTypeResult:
    ancestry = index_type_ancestry(document.type_contracts)
    checks = ExportTypeChecks(ancestry)
    definitions = (
        {} if document.graph.subgraphs is MISSING else document.graph.subgraphs
    )
    for name, body in definitions.items():
        if body.outputs is MISSING:
            continue
        for export_name, value in body.outputs.items():
            checks.check(
                body,
                value,
                ("graph", "subgraphs", name, "outputs", export_name, "type"),
            )
    result = ExportTypeResult(
        checks.collector.finish(),
        len(definitions),
        checks.exports,
        len(document.type_contracts),
        len(ancestry.intervals),
        checks.compared,
        checks.unresolved,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            export_types_valid=result.export_types_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            definitions=result.definitions_checked,
            exports=result.exports_checked,
            contracts=result.contracts_checked,
            types_indexed=result.types_indexed,
            compared=result.exports_compared,
            unresolved=result.exports_unresolved,
        )
    return result
