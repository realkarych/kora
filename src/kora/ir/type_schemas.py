from dataclasses import dataclass

from kora.ir.contracts import SCHEMA_DIALECT, BuiltinTypeContract
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject
from kora.ir.schema_catalogue import builtin_schema
from kora.ir.schema_keywords import KeywordValidation
from kora.ir.schema_locations import (
    Location,
    SavedReferences,
    contract_location,
    schema_sites,
)
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
REFERENCE_KEYWORDS: frozenset[str] = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


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
        if isinstance(contract, BuiltinTypeContract):
            root = builtin_schema(name, contract.builtin_revision)
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
        else:
            roots[name] = contract.schema_
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
        for schema, location in schema_sites(root, contract_location(name)):
            schema_count += 1
            if isinstance(schema, bool):
                continue
            keywords.check(schema, location)
            for keyword, value in schema.items():
                if keyword == "$id" and schema is not root:
                    fault(location.child(keyword))
                elif (
                    keyword == "$schema"
                    and isinstance(value, str)
                    and value != SCHEMA_DIALECT
                ):
                    fault(location.child(keyword))
                elif keyword in REFERENCE_KEYWORDS and isinstance(value, str):
                    reference_count += 1
                    if not references.resolves(name, value):
                        fault(location.child(keyword))
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
