from dataclasses import dataclass

from pydantic import MISSING

from kora.cel import SchemaError, SchemaTypes
from kora.cel.meter import Meter
from kora.cel.types import CelType, Dyn, Kind, Primitive, Record
from kora.ir.declarations import ParameterTarget
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.schema_catalogue import contract_schema
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.target_parameter_types.completed",
        COMPONENT,
        Level.INFO,
        "known target parameter type validation completed",
        {
            "known_target_parameter_types_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "targets": FieldKind.INTEGER,
            "resolved": FieldKind.INTEGER,
            "unresolved": FieldKind.INTEGER,
            "schema_steps": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class KnownTargetParameterTypeResult:
    diagnostics: DiagnosticCollection
    parameter_targets_checked: int
    parameter_types_resolved: int
    parameter_types_unresolved: int
    schema_steps: int

    @property
    def known_target_parameter_types_valid(self) -> bool:
        return self.diagnostics.valid and self.parameter_types_unresolved == 0


def declared_parameter_type(
    document: GraphDocument, name: str, meter: Meter
) -> CelType | None:
    if document.graph.params is MISSING:
        return None
    roots = {
        "urn:kora:artifact:" + key.replace("/", ":"): root
        for key, contract in document.type_contracts.items()
        if (root := contract_schema(key, contract)) is not None
    }
    schemas = SchemaTypes(roots.get, meter)
    try:
        mapped = schemas.document(document.graph.params)
    except SchemaError:
        return None
    if not isinstance(mapped, Record):
        return None
    member = mapped.fields.get(name)
    if member is None or isinstance(member.type, Dyn):
        return None
    return member.type


def validate_known_target_parameter_types(
    document: GraphDocument,
) -> KnownTargetParameterTypeResult:
    collector = DiagnosticCollector()
    meter = Meter()
    targets = 0
    resolved = 0
    target = document.graph.target
    if target is not MISSING and isinstance(target.key, ParameterTarget):
        targets = 1
        selected_type = declared_parameter_type(document, target.key.param, meter)
        if selected_type is not None:
            resolved = 1
            if not (
                isinstance(selected_type, Primitive)
                and selected_type.kind is Kind.STRING
            ):
                collector.add(
                    Diagnostic(
                        code="IR020",
                        message=(
                            "The declared target parameter must have a string type."
                        ),
                        path=("graph", "target", "key", "param"),
                        location=safe_source(document.graph.source),
                    )
                )
    result = KnownTargetParameterTypeResult(
        collector.finish(), targets, resolved, targets - resolved, meter.steps
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            known_target_parameter_types_valid=result.known_target_parameter_types_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            targets=result.parameter_targets_checked,
            resolved=result.parameter_types_resolved,
            unresolved=result.parameter_types_unresolved,
            schema_steps=result.schema_steps,
        )
    return result
