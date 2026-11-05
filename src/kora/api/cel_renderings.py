from asyncio import CancelledError
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from kora.cel import CheckedExpression
from kora.cel_eval.errors import EvaluatorUnavailable, Fault
from kora.cel_eval.thread import Evaluator
from kora.ir.expression_fields import FieldPath

PathT = TypeVar("PathT", bound=FieldPath)
PathT_co = TypeVar("PathT_co", bound=FieldPath, covariant=True)


class CheckedRenderingSite(Protocol[PathT_co]):
    @property
    def path(self) -> PathT_co: ...

    @property
    def checked(self) -> CheckedExpression: ...


class RenderingRefusal(StrEnum):
    INVALID_ALLOWANCE = "invalid_allowance"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    NATIVE_REFUSED = "rendering_refused"


@dataclass(frozen=True, slots=True)
class IncompleteRenderings(Generic[PathT]):
    reason: RenderingRefusal
    path: PathT | None = field(default=None, repr=False)
    fault: Fault | None = None


@dataclass(frozen=True, slots=True)
class CompiledRenderings:
    expressions: int


async def compile_rendering_sites(
    sites: tuple[CheckedRenderingSite[PathT], ...],
    evaluator: Evaluator,
    *,
    max_rendering_characters: int,
) -> CompiledRenderings | IncompleteRenderings[PathT]:
    remaining = max_rendering_characters
    for site in sites:
        remaining -= len(site.checked.rendering)
        if remaining < 0:
            return IncompleteRenderings(RenderingRefusal.CAPACITY_EXHAUSTED)
    pending = deque(sites)
    del sites
    site = None
    compiled = 0
    try:
        while pending:
            site = pending.popleft()
            result = await evaluator.validate(site.checked)
            if not result.valid:
                return IncompleteRenderings(
                    RenderingRefusal.NATIVE_REFUSED, site.path, result.fault
                )
            compiled += 1
            site = None
        return CompiledRenderings(compiled)
    except (EvaluatorUnavailable, CancelledError) as error:
        error.__traceback__ = None
        raise
    finally:
        pending.clear()
        site = None
