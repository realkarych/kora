from kora.ir.standard_schema_adapter import evaluate
from kora.ir.standard_schema_admission import admit
from kora.ir.standard_schema_policy import (
    SchemaDecision,
    SchemaDomain,
    SchemaRefused,
    bind_policy,
)
from kora.ir.standard_schema_scan import SourceScan
from kora.ir.standard_schema_support import COMPONENT, prepare_source
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.standard_schema_validation.completed",
        COMPONENT,
        Level.DEBUG,
        "standard schema validation completed",
        {"outcome": FieldKind.STRING, "reason": FieldKind.STRING}
        | dict.fromkeys(
            (
                "value_nodes",
                "schema_nodes",
                "schema_positions",
                "reference_edges",
                "expanded_calls",
                "validation_score",
                "variable_bytes",
                "auxiliary_entries",
            ),
            FieldKind.INTEGER,
        ),
    )
)


def value_decision(
    value: object, source: object, domain: SchemaDomain, binding: object
) -> SchemaDecision:
    limits = bind_policy(domain, binding)
    value_stats = SourceScan(limits, domain, schema=False).measure(value)
    source_stats, sources = prepare_source(source, limits)
    accounting = admit(sources, value_stats, source_stats, domain)
    valid = evaluate(value, source, sources)
    return SchemaDecision("valid" if valid else "invalid", accounting=accounting)


def validate_standard_schema(
    value: object, source: object, domain: SchemaDomain, binding: object
) -> SchemaDecision:
    try:
        result = value_decision(value, source, domain, binding)
    except SchemaRefused as error:
        result = SchemaDecision("refused", error.reason)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        counts = result.accounting
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            outcome=result.outcome,
            reason=result.reason,
            value_nodes=counts.value_nodes,
            schema_nodes=counts.schema_nodes,
            schema_positions=counts.schema_positions,
            reference_edges=counts.reference_edges,
            expanded_calls=counts.expanded_calls,
            validation_score=counts.validation_score,
            variable_bytes=counts.variable_bytes,
            auxiliary_entries=counts.auxiliary_entries,
        )
    return result
