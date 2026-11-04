from dataclasses import dataclass
from typing import Literal

from kora.api.exchange import COMPONENT
from kora.cel_eval.errors import EvaluatorUnavailable
from kora.cel_eval.evaluate import Compilation
from kora.cel_eval.thread import Evaluator
from kora.ir.approval_compilation import (
    ApprovalPrerequisite,
    CheckedApproval,
    check_root_approval,
)
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.approval_rendering.completed",
        COMPONENT,
        Level.DEBUG,
        "approval rendering check completed",
        {"status": FieldKind.STRING},
    )
)


@dataclass(frozen=True)
class CompiledApprovalRendering:
    approval: CheckedApproval


def emit_completion(
    status: ApprovalPrerequisite
    | Literal[
        "typed_refused",
        "evaluator_unavailable",
        "rendering_refused",
        "rendering_compiled",
    ],
) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(COMPLETED, status=str(status))


async def compile_root_approval_rendering(
    document: GraphDocument, name: str, evaluator: Evaluator
) -> (
    CompiledApprovalRendering
    | DiagnosticCollection
    | ApprovalPrerequisite
    | Compilation
):
    approval = check_root_approval(document, name)
    if isinstance(approval, ApprovalPrerequisite):
        emit_completion(approval)
        return approval
    if isinstance(approval, DiagnosticCollection):
        emit_completion("typed_refused")
        return approval
    try:
        compilation = await evaluator.validate(approval.checked)
    except EvaluatorUnavailable:
        emit_completion("evaluator_unavailable")
        raise
    if not compilation.valid:
        emit_completion("rendering_refused")
        return compilation
    emit_completion("rendering_compiled")
    return CompiledApprovalRendering(approval)
