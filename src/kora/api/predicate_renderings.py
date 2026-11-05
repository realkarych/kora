from typing import TypeVar

from kora.api.cel_renderings import (
    CheckedRenderingSite,
    CompiledRenderings,
    IncompleteRenderings,
    RenderingRefusal,
    compile_rendering_sites,
)
from kora.api.exchange import COMPONENT
from kora.cel_eval.errors import EvaluatorUnavailable
from kora.cel_eval.thread import Evaluator
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.ir.retry_expressions import RetryExpressionResult, checked_retry_sites
from kora.ir.trigger_expressions import (
    SensorTriggerExpressionResult,
    checked_sensor_trigger_sites,
)
from kora.ir.wait_expressions import WaitExpressionResult, checked_wait_sites
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

WAIT_COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.wait_renderings.completed",
        COMPONENT,
        Level.DEBUG,
        "wait rendering inventory check completed",
        {"status": FieldKind.STRING},
    )
)
RETRY_COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.retry_renderings.completed",
        COMPONENT,
        Level.DEBUG,
        "retry rendering inventory check completed",
        {"status": FieldKind.STRING},
    )
)
SENSOR_COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.sensor_trigger_renderings.completed",
        COMPONENT,
        Level.DEBUG,
        "sensor trigger rendering inventory check completed",
        {"status": FieldKind.STRING},
    )
)

PrerequisiteT = TypeVar(
    "PrerequisiteT",
    WaitExpressionResult,
    RetryExpressionResult,
    SensorTriggerExpressionResult,
)
RenderingCheck = CompiledRenderings | IncompleteRenderings[FieldPath]


def invalid_allowances(declarations: int, characters: int) -> bool:
    return (
        type(declarations) is not int
        or declarations < 0
        or type(characters) is not int
        or characters < 0
    )


def emit_completion(event: EventSpec, status: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(event, status=status)


def announce(event: EventSpec, result: RenderingCheck) -> RenderingCheck:
    emit_completion(
        event,
        result.reason.value if isinstance(result, IncompleteRenderings) else "compiled",
    )
    return result


async def compile_predicate_inventory(
    inventory: tuple[CheckedRenderingSite[FieldPath], ...] | PrerequisiteT | None,
    evaluator: Evaluator,
    *,
    max_rendering_characters: int,
    event: EventSpec,
) -> RenderingCheck | PrerequisiteT:
    if inventory is None:
        return announce(
            event, IncompleteRenderings(RenderingRefusal.CAPACITY_EXHAUSTED)
        )
    if not isinstance(inventory, tuple):
        emit_completion(event, "typed_refused")
        return inventory
    verification = compile_rendering_sites(
        inventory, evaluator, max_rendering_characters=max_rendering_characters
    )
    del inventory
    try:
        return announce(event, await verification)
    except EvaluatorUnavailable:
        emit_completion(event, "evaluator_unavailable")
        raise


async def compile_wait_renderings(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_declarations: int,
    max_rendering_characters: int,
) -> RenderingCheck | WaitExpressionResult:
    if invalid_allowances(max_declarations, max_rendering_characters):
        return announce(
            WAIT_COMPLETED, IncompleteRenderings(RenderingRefusal.INVALID_ALLOWANCE)
        )
    inventory = checked_wait_sites(document, max_declarations=max_declarations)
    verification = compile_predicate_inventory(
        inventory,
        evaluator,
        max_rendering_characters=max_rendering_characters,
        event=WAIT_COMPLETED,
    )
    del document, inventory
    return await verification


async def compile_retry_renderings(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_declarations: int,
    max_rendering_characters: int,
) -> RenderingCheck | RetryExpressionResult:
    if invalid_allowances(max_declarations, max_rendering_characters):
        return announce(
            RETRY_COMPLETED, IncompleteRenderings(RenderingRefusal.INVALID_ALLOWANCE)
        )
    inventory = checked_retry_sites(document, max_declarations=max_declarations)
    verification = compile_predicate_inventory(
        inventory,
        evaluator,
        max_rendering_characters=max_rendering_characters,
        event=RETRY_COMPLETED,
    )
    del document, inventory
    return await verification


async def compile_sensor_trigger_renderings(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    max_triggers: int,
    max_rendering_characters: int,
) -> RenderingCheck | SensorTriggerExpressionResult:
    if invalid_allowances(max_triggers, max_rendering_characters):
        return announce(
            SENSOR_COMPLETED, IncompleteRenderings(RenderingRefusal.INVALID_ALLOWANCE)
        )
    inventory = checked_sensor_trigger_sites(document, max_triggers=max_triggers)
    verification = compile_predicate_inventory(
        inventory,
        evaluator,
        max_rendering_characters=max_rendering_characters,
        event=SENSOR_COMPLETED,
    )
    del document, inventory
    return await verification
