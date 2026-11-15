from collections.abc import Mapping

from kora.ir.standard_schema_policy import (
    SchemaDomain,
    SchemaRefused,
    SourceAccounting,
    SourceDecision,
    bind_policy,
    require,
)
from kora.ir.standard_schema_scan import SourceScan, preparation_score
from kora.ir.standard_schema_source import Sources
from kora.ir.standard_schema_values import Statistics
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPONENT: str = CATALOG.register_component("ir")
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.standard_schema_source.completed",
        COMPONENT,
        Level.DEBUG,
        "standard schema source check completed",
        {"outcome": FieldKind.STRING, "reason": FieldKind.STRING}
        | dict.fromkeys(
            (
                "source_bytes",
                "source_nodes",
                "schema_positions",
                "reference_edges",
                "schema_documents",
                "preparation_score",
            ),
            FieldKind.INTEGER,
        ),
    )
)


def check_graph(sources: Sources) -> None:
    parents: list[list[int]] = [[] for _ in sources.sites]
    for current, site in enumerate(sources.sites):
        site.remaining = len(site.edges)
        for target, _ in site.edges:
            parents[target].append(current)
    ready = [index for index, site in enumerate(sources.sites) if not site.remaining]
    processed = 0
    cap = sources.limits["max_call_depth"]
    while ready:
        current = ready.pop()
        child = sources.sites[current]
        processed += 1
        for parent in parents[current]:
            site = sources.sites[parent]
            site.depth = min(cap + 1, max(site.depth, child.depth + 1))
            site.remaining -= 1
            if not site.remaining:
                ready.append(parent)
    require(processed == len(sources.sites))
    require(
        all(sources.sites[root].depth <= cap for root in sources.roots()), capacity=True
    )


def prepare_source(
    source: object, limits: Mapping[str, int]
) -> tuple[Statistics, Sources]:
    measured = SourceScan(limits).measure(source)
    score = preparation_score(measured, limits["max_validation_score"])
    require(score <= limits["max_validation_score"], capacity=True)
    sources = Sources(source, limits)
    check_graph(sources)
    return measured, sources


def source_checks(
    source: object, domain: SchemaDomain, binding: object
) -> SourceAccounting:
    limits = bind_policy(domain, binding)
    measured, sources = prepare_source(source, limits)
    return SourceAccounting(
        measured.tree.encoded,
        measured.tree.nodes,
        len(sources.sites),
        len(sources.references),
        len(sources.documents),
        preparation_score(measured, limits["max_validation_score"]),
    )


def check_standard_schema_source(
    source: object, domain: SchemaDomain, binding: object
) -> SourceDecision:
    try:
        result = SourceDecision(
            "source_checks_completed", accounting=source_checks(source, domain, binding)
        )
    except SchemaRefused as error:
        result = SourceDecision("refused", error.reason)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        counts = result.accounting
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            outcome=result.outcome,
            reason=result.reason,
            source_bytes=counts.source_bytes,
            source_nodes=counts.source_nodes,
            schema_positions=counts.schema_positions,
            reference_edges=counts.reference_edges,
            schema_documents=counts.schema_documents,
            preparation_score=counts.preparation_score,
        )
    return result
