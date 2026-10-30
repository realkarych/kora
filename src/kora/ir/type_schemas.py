from dataclasses import dataclass

from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject
from kora.ir.schema_catalogue import contract_schema
from kora.ir.schema_keywords import KeywordValidation
from kora.ir.schema_locations import (
    Location,
    SavedReferences,
    contract_location,
)
from kora.ir.schema_validation import validate_schema
from kora.ir.type_declarations import COMPONENT, contract_path
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.type_schemas.completed",
        COMPONENT,
        Level.INFO,
        "type schema validation completed",
        {
            "schemas_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "contracts": FieldKind.INTEGER,
            "schemas": FieldKind.INTEGER,
            "references": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class TypeSchemaResult:
    diagnostics: DiagnosticCollection
    contracts_checked: int
    schemas_checked: int
    references_checked: int

    @property
    def schemas_valid(self) -> bool:
        return self.diagnostics.valid


def saved_roots(
    document: GraphDocument, collector: DiagnosticCollector
) -> dict[str, JsonObject]:
    roots: dict[str, JsonObject] = {}
    for name, contract in document.type_contracts.items():
        root = contract_schema(name, contract)
        if root is None:
            collector.add(
                Diagnostic(
                    code="IR022",
                    message="The built-in name and revision are not published.",
                    path=contract_path(name, "builtin_revision"),
                )
            )
        else:
            roots[name] = root
    return roots


def validate_type_schemas(document: GraphDocument) -> TypeSchemaResult:
    collector = DiagnosticCollector()

    def fault(location: Location) -> None:
        collector.add(
            Diagnostic(
                code="IR022",
                message="The saved type schema or reference is invalid.",
                path=location.path,
            )
        )

    roots = saved_roots(document, collector)
    references = SavedReferences(roots)
    keywords = KeywordValidation(fault)
    schema_count = 0
    reference_count = 0
    for name, root in roots.items():
        counts = validate_schema(
            root, contract_location(name), references, keywords, fault
        )
        schema_count += counts.schemas
        reference_count += counts.references
    result = TypeSchemaResult(
        collector.finish(), len(document.type_contracts), schema_count, reference_count
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            schemas_valid=result.schemas_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            contracts=result.contracts_checked,
            schemas=schema_count,
            references=reference_count,
        )
    return result
