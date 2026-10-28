import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from kora.executor import CONTENT_REFUSED
from kora.executor.failures import Phase, deadline_due, fail, misconfigured
from kora.executor.guard import PhaseGuard
from kora.ir.globs import (
    Fuel,
    FuelExhausted,
    GlobInvalid,
    PathGlob,
    compile_globs,
    folded,
    folded_subject,
)
from kora.ir.node import ErrorClass
from kora.observability import ComponentLogger
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND

CHECK_BUDGET: Final[int] = 2**34
PATH_CAP: Final[int] = 2**24
SLICE_UNITS: Final[int] = 2**28
CHECK_BOUND_NS: Final[int] = 3600 * NANOSECONDS_PER_SECOND


@dataclass(frozen=True)
class FuelBounds:
    budget: int = CHECK_BUDGET
    per_path: int = PATH_CAP
    slice_units: int = SLICE_UNITS


class Finding(StrEnum):
    CLEAR = "clear"
    RULE = "rule"
    FUEL = "fuel"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CheckResult:
    finding: Finding
    spent: int


def compiled_globs(texts: tuple[str, ...], phase: Phase) -> tuple[PathGlob, ...]:
    try:
        return compile_globs(texts)
    except GlobInvalid:
        misconfigured("protected_paths_invalid", phase)


def path_finding(
    globs: Sequence[PathGlob], path: str, cap: int
) -> tuple[Finding | None, int]:
    fuel = Fuel(cap)
    subject = folded_subject(folded(path))
    try:
        matched = any(glob.matches(subject, fuel) for glob in globs)
    except FuelExhausted:
        return Finding.FUEL, fuel.spent
    return (Finding.RULE if matched else None), fuel.spent


def check_paths(
    globs: Sequence[PathGlob],
    paths: Sequence[str],
    bounds: FuelBounds,
    cancelled: threading.Event,
) -> CheckResult:
    spent = 0
    next_slice = bounds.slice_units
    for path in paths:
        if spent >= next_slice:
            if cancelled.is_set():
                return CheckResult(Finding.CANCELLED, spent)
            next_slice = spent + bounds.slice_units
        finding, used = path_finding(
            globs, path, min(bounds.per_path, bounds.budget - spent)
        )
        spent += used
        if finding is not None:
            return CheckResult(finding, spent)
    return CheckResult(Finding.CLEAR, spent)


async def checked_off_loop(
    globs: Sequence[PathGlob], paths: Sequence[str], bounds: FuelBounds
) -> CheckResult:
    loop = asyncio.get_running_loop()
    done: asyncio.Future[CheckResult] = loop.create_future()
    cancelled = threading.Event()

    def deliver(result: CheckResult) -> None:
        if not done.done():
            done.set_result(result)

    def work() -> None:
        result = check_paths(globs, paths, bounds, cancelled)
        if not loop.is_closed():
            loop.call_soon_threadsafe(deliver, result)

    worker = threading.Thread(target=work, name="kora-protected-paths", daemon=True)
    worker.start()
    try:
        return await asyncio.shield(done)
    finally:
        cancelled.set()


async def check_protected(
    globs: Sequence[PathGlob],
    paths: Sequence[str],
    bounds: FuelBounds,
    phase: Phase,
    logger: ComponentLogger,
    guard: PhaseGuard,
) -> CheckResult:
    unique = tuple(dict.fromkeys(paths))
    if not globs or not unique:
        return CheckResult(Finding.CLEAR, 0)
    remaining_ns = guard.bound_ns(CHECK_BOUND_NS, phase)
    try:
        result = await asyncio.wait_for(
            checked_off_loop(globs, unique, bounds),
            remaining_ns / NANOSECONDS_PER_SECOND,
        )
    except TimeoutError:
        deadline_due(phase)
    if result.finding is Finding.CANCELLED:
        deadline_due(phase)
    if result.finding in (Finding.RULE, Finding.FUEL):
        logger.emit(
            CONTENT_REFUSED,
            phase=phase.value,
            failure_class=ErrorClass.PROTECTED_PATH_MODIFIED.value,
            code="protected_path_modified",
            cause=result.finding.value,
            count=1,
        )
        fail(ErrorClass.PROTECTED_PATH_MODIFIED, "protected_path_modified", phase)
    return result
