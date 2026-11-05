from kora.api.cel_renderings import (
    CompiledRenderings,
    IncompleteRenderings,
    RenderingRefusal,
    compile_rendering_sites,
)
from kora.api.exchange import COMPONENT
from kora.cel_eval.errors import EvaluatorUnavailable
from kora.cel_eval.thread import Evaluator
from kora.ir.branch_expressions import (
    BranchExpressionResult,
    CheckedBranchSite,
    checked_branch_sites,
)
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.branch_renderings.completed",
        COMPONENT,
        Level.DEBUG,
        "branch rendering inventory check completed",
        {"status": FieldKind.STRING},
    )
)

RenderingCheck = CompiledRenderings | IncompleteRenderings[FieldPath]


def emit_completion(status: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(COMPLETED, status=status)


def announce(result: RenderingCheck) -> RenderingCheck:
    emit_completion(
        result.reason.value if isinstance(result, IncompleteRenderings) else "compiled"
    )
    return result


async def compile_branch_sites(
    sites: tuple[CheckedBranchSite, ...],
    evaluator: Evaluator,
    *,
    max_rendering_characters: int,
) -> RenderingCheck:
    verification = compile_rendering_sites(
        sites, evaluator, max_rendering_characters=max_rendering_characters
    )
    del sites
    return announce(await verification)


async def compile_branch_renderings(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_declarations: int,
    max_rendering_characters: int,
) -> RenderingCheck | BranchExpressionResult:
    if (
        type(max_declarations) is not int
        or max_declarations < 0
        or type(max_rendering_characters) is not int
        or max_rendering_characters < 0
    ):
        return announce(IncompleteRenderings(RenderingRefusal.INVALID_ALLOWANCE))
    inventory = checked_branch_sites(document, max_declarations=max_declarations)
    if inventory is None:
        return announce(IncompleteRenderings(RenderingRefusal.CAPACITY_EXHAUSTED))
    if isinstance(inventory, BranchExpressionResult):
        emit_completion("typed_refused")
        return inventory
    verification = compile_branch_sites(
        inventory, evaluator, max_rendering_characters=max_rendering_characters
    )
    del document, inventory
    try:
        return await verification
    except EvaluatorUnavailable:
        emit_completion("evaluator_unavailable")
        raise
