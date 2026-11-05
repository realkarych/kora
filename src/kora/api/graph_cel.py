from dataclasses import dataclass, field
from enum import StrEnum

from kora.api.cel_pattern_validation import (
    IncompletePatternCheck,
    prepare_patterns,
    validate_prepared_patterns,
)
from kora.api.cel_renderings import IncompleteRenderings, compile_rendering_sites
from kora.api.exchange import COMPONENT
from kora.cel_eval.thread import Evaluator
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_cel_facts import GraphCelFacts, stage_graph_cel_facts
from kora.ir.approval_compilation import ApprovalPrerequisite
from kora.ir.cel_inventory_admission import (
    CheckedInventoryAdmission,
    InventoryAdmissionExhausted,
)
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.ir.graph_cel_inventory import TypedRefusal, checked_graph_sites
from kora.ir.retry_expressions import InfrastructureRetryPrerequisite
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


@dataclass(frozen=True, slots=True)
class GraphCelChecks:
    compiled_fields: int
    diagnostics: DiagnosticCollection
    facts: GraphCelFacts | None = field(default=None, repr=False)


GraphCelResult = (
    GraphCelChecks
    | GraphCelRefusal
    | TypedRefusal
    | IncompleteRenderings[FieldPath]
    | IncompletePatternCheck
)


def announce(result: GraphCelResult) -> GraphCelResult:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        if isinstance(result, GraphCelChecks):
            status = "complete"
        elif isinstance(result, IncompleteRenderings):
            status = result.reason.value
        elif isinstance(
            result,
            GraphCelRefusal
            | ApprovalPrerequisite
            | InfrastructureRetryPrerequisite
            | IncompletePatternCheck,
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
    max_facts_bytes: int | None = None,
    max_typed_work: int | None = None,
    max_typed_bytes: int | None = None,
) -> GraphCelResult:
    if (
        any(
            type(limit) is not int or limit < 0
            for limit in (max_declarations, max_triggers, max_rendering_characters)
        )
        or type(max_pattern_work) is not int
        or max_pattern_work <= 0
        or any(
            type(limit) is not int or limit <= 0
            for limit in (max_facts_bytes, max_typed_work, max_typed_bytes)
            if limit is not None
        )
        or (max_typed_work is None) != (max_typed_bytes is None)
    ):
        return announce(GraphCelRefusal.INVALID_ALLOWANCE)
    admission = (
        CheckedInventoryAdmission(max_typed_work, max_typed_bytes)
        if max_typed_work is not None and max_typed_bytes is not None
        else None
    )
    try:
        inventory = checked_graph_sites(
            document, max_declarations, max_triggers, admission=admission
        )
    except InventoryAdmissionExhausted:
        return announce(GraphCelRefusal.CAPACITY_EXHAUSTED)
    del document, admission
    if inventory is None:
        return announce(GraphCelRefusal.CAPACITY_EXHAUSTED)
    if not isinstance(inventory, tuple):
        return announce(inventory)
    prepared = prepare_patterns(inventory, max_pattern_work)
    if isinstance(prepared, IncompletePatternCheck):
        return announce(prepared)
    staged = None
    try:
        if max_facts_bytes is not None:
            try:
                staged = stage_graph_cel_facts(inventory, max_facts_bytes)
            except GraphValueRejected as error:
                if error.reason is not GraphValueFailure.BUDGET:
                    raise
                return announce(GraphCelRefusal.CAPACITY_EXHAUSTED)
        compilation = compile_rendering_sites(
            inventory, evaluator, max_rendering_characters=max_rendering_characters
        )
        del inventory
        compiled = await compilation
        if isinstance(compiled, IncompleteRenderings):
            return announce(compiled)
        checked = await validate_prepared_patterns(prepared, evaluator)
        if isinstance(checked, IncompletePatternCheck):
            return announce(checked)
        return announce(
            GraphCelChecks(
                compiled.expressions,
                checked.diagnostics,
                staged if checked.diagnostics.valid else None,
            )
        )
    finally:
        staged = None
        prepared.clear()
