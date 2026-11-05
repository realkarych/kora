from dataclasses import dataclass, field

from kora.api.cel_renderings import (
    IncompleteRenderings,
    RenderingRefusal,
    compile_rendering_sites,
)
from kora.api.exchange import COMPONENT
from kora.cel_eval.errors import EvaluatorUnavailable, Fault
from kora.cel_eval.thread import Evaluator
from kora.ir.approval_compilation import (
    ApprovalPrerequisite,
    CheckedApprovalSite,
    checked_approval_sites,
)
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.approval_renderings.completed",
        COMPONENT,
        Level.DEBUG,
        "approval rendering inventory check completed",
        {"status": FieldKind.STRING},
    )
)


@dataclass(frozen=True, slots=True)
class IncompleteApprovalRenderings:
    reason: RenderingRefusal
    path: tuple[str, ...] | None = field(default=None, repr=False)
    fault: Fault | None = None


@dataclass(frozen=True, slots=True)
class CompiledApprovalRenderings:
    expressions: int


RenderingCheck = CompiledApprovalRenderings | IncompleteApprovalRenderings


def emit_completion(status: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(COMPLETED, status=status)


def announce(result: RenderingCheck) -> RenderingCheck:
    emit_completion(
        result.reason.value
        if isinstance(result, IncompleteApprovalRenderings)
        else "compiled"
    )
    return result


async def compile_approval_sites(
    sites: tuple[CheckedApprovalSite, ...],
    evaluator: Evaluator,
    *,
    max_rendering_characters: int,
) -> RenderingCheck:
    verification = compile_rendering_sites(
        sites, evaluator, max_rendering_characters=max_rendering_characters
    )
    del sites
    result = await verification
    return announce(
        IncompleteApprovalRenderings(result.reason, result.path, result.fault)
        if isinstance(result, IncompleteRenderings)
        else CompiledApprovalRenderings(result.expressions)
    )


async def compile_approval_renderings(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_declarations: int,
    max_rendering_characters: int,
) -> RenderingCheck | DiagnosticCollection | ApprovalPrerequisite:
    if (
        type(max_declarations) is not int
        or max_declarations < 0
        or type(max_rendering_characters) is not int
        or max_rendering_characters < 0
    ):
        return announce(
            IncompleteApprovalRenderings(RenderingRefusal.INVALID_ALLOWANCE)
        )
    inventory = checked_approval_sites(document, max_declarations=max_declarations)
    if inventory is None:
        return announce(
            IncompleteApprovalRenderings(RenderingRefusal.CAPACITY_EXHAUSTED)
        )
    if isinstance(inventory, ApprovalPrerequisite | DiagnosticCollection):
        emit_completion(
            inventory.value
            if isinstance(inventory, ApprovalPrerequisite)
            else "typed_refused"
        )
        return inventory
    verification = compile_approval_sites(
        inventory, evaluator, max_rendering_characters=max_rendering_characters
    )
    del document, inventory
    try:
        return await verification
    except EvaluatorUnavailable:
        emit_completion("evaluator_unavailable")
        raise
