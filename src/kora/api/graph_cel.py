from dataclasses import dataclass
from enum import StrEnum
from functools import partial

from pydantic import MISSING

from kora.api.cel_pattern_validation import (
    CheckedPatternSite,
    IncompletePatternCheck,
    prepare_patterns,
    validate_prepared_patterns,
)
from kora.api.cel_renderings import IncompleteRenderings, compile_rendering_sites
from kora.api.exchange import COMPONENT
from kora.cel_eval.thread import Evaluator
from kora.ir.approval_compilation import (
    ApprovalPrerequisite,
    CheckedApprovalSite,
    checked_approval_sites,
)
from kora.ir.branch_expressions import BranchExpressionResult, checked_branch_sites
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.ir.retry_expressions import RetryExpressionResult, checked_retry_sites
from kora.ir.trigger_expressions import (
    SensorTriggerExpressionResult,
    checked_sensor_trigger_sites,
)
from kora.ir.wait_expressions import WaitExpressionResult, checked_wait_sites
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.graph_cel.completed",
        COMPONENT,
        Level.DEBUG,
        "Graph CEL verification completed",
        {"status": FieldKind.STRING},
    )
)


class GraphCelRefusal(StrEnum):
    INVALID_ALLOWANCE = "invalid_allowance"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    INFRASTRUCTURE_CONTEXT_UNRESOLVED = "infrastructure_context_unresolved"


@dataclass(frozen=True, slots=True)
class GraphCelChecks:
    compiled_fields: int
    diagnostics: DiagnosticCollection


TypedRefusal = (
    GraphCelRefusal
    | ApprovalPrerequisite
    | DiagnosticCollection
    | BranchExpressionResult
    | RetryExpressionResult
    | WaitExpressionResult
    | SensorTriggerExpressionResult
)
GraphCelResult = (
    GraphCelChecks
    | TypedRefusal
    | IncompleteRenderings[FieldPath]
    | IncompletePatternCheck
)


def checked_graph_sites(
    document: GraphDocument, max_declarations: int, max_triggers: int
) -> tuple[CheckedPatternSite, ...] | TypedRefusal:
    infrastructure = document.runtime.infrastructure_retry
    if infrastructure is not MISSING and infrastructure.predicate is not MISSING:
        return GraphCelRefusal.INFRASTRUCTURE_CONTEXT_UNRESOLVED
    triggers = document.graph.triggers
    if triggers is not MISSING and len(triggers) > max_triggers:
        return GraphCelRefusal.CAPACITY_EXHAUSTED
    producers = (
        partial(checked_branch_sites, max_declarations=max_declarations),
        partial(checked_approval_sites, max_declarations=max_declarations),
        partial(checked_retry_sites, max_declarations=max_declarations),
        partial(checked_wait_sites, max_declarations=max_declarations),
        partial(checked_sensor_trigger_sites, max_triggers=max_triggers),
    )
    sites: list[CheckedPatternSite] = []
    for produce in producers:
        inventory = produce(document)
        if inventory is None:
            return GraphCelRefusal.CAPACITY_EXHAUSTED
        if not isinstance(inventory, tuple):
            return inventory
        sites.extend(
            CheckedPatternSite(
                site.path,
                site.checked,
                site.authored if isinstance(site, CheckedApprovalSite) else True,
            )
            for site in inventory
        )
    return tuple(sites)


def announce(result: GraphCelResult) -> GraphCelResult:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        if isinstance(result, GraphCelChecks):
            status = "complete"
        elif isinstance(result, IncompleteRenderings):
            status = result.reason.value
        elif isinstance(
            result, GraphCelRefusal | ApprovalPrerequisite | IncompletePatternCheck
        ):
            status = result.value
        else:
            status = "typed_refused"
        diagnostics.logger(COMPONENT).emit(COMPLETED, status=status)
    return result


async def verify_graph_cel(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_declarations: int,
    max_triggers: int,
    max_rendering_characters: int,
    max_pattern_work: int,
) -> GraphCelResult:
    if (
        any(
            type(limit) is not int or limit < 0
            for limit in (max_declarations, max_triggers, max_rendering_characters)
        )
        or type(max_pattern_work) is not int
        or max_pattern_work <= 0
    ):
        return announce(GraphCelRefusal.INVALID_ALLOWANCE)
    inventory = checked_graph_sites(document, max_declarations, max_triggers)
    del document
    if not isinstance(inventory, tuple):
        return announce(inventory)
    prepared = prepare_patterns(inventory, max_pattern_work)
    if isinstance(prepared, IncompletePatternCheck):
        return announce(prepared)
    compilation = compile_rendering_sites(
        inventory, evaluator, max_rendering_characters=max_rendering_characters
    )
    del inventory
    try:
        compiled = await compilation
        if isinstance(compiled, IncompleteRenderings):
            return announce(compiled)
        checked = await validate_prepared_patterns(prepared, evaluator)
        if isinstance(checked, IncompletePatternCheck):
            return announce(checked)
        return announce(GraphCelChecks(compiled.expressions, checked.diagnostics))
    finally:
        prepared.clear()
