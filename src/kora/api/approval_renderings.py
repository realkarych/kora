from asyncio import CancelledError
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

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


class RenderingRefusal(StrEnum):
    INVALID_ALLOWANCE = "invalid_allowance"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    NATIVE_REFUSED = "rendering_refused"


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
    remaining = max_rendering_characters
    for site in sites:
        remaining -= len(site.checked.rendering)
        if remaining < 0:
            return announce(
                IncompleteApprovalRenderings(RenderingRefusal.CAPACITY_EXHAUSTED)
            )
    pending = deque(sites)
    del sites
    site = None
    compiled = 0
    try:
        while pending:
            site = pending.popleft()
            result = await evaluator.validate(site.checked)
            if not result.valid:
                return announce(
                    IncompleteApprovalRenderings(
                        RenderingRefusal.NATIVE_REFUSED, site.path, result.fault
                    )
                )
            compiled += 1
            site = None
        return announce(CompiledApprovalRenderings(compiled))
    except (EvaluatorUnavailable, CancelledError) as error:
        error.__traceback__ = None
        raise
    finally:
        pending.clear()
        site = None


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
