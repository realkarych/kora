import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from kora.executor import STEP_FAILED, STEP_FINISHED
from kora.executor.captures import Captures, capture_limit, parsed
from kora.executor.failures import (
    ExecutorError,
    Phase,
    StepPosition,
    misconfigured,
)
from kora.executor.guard import PhaseGuard
from kora.executor.launcher import Ending, LaunchSpec, StepLauncher, StepOutcome
from kora.ir.node import ErrorClass
from kora.observability import ComponentLogger
from kora.protocols.deadlines import duration_ns
from kora.protocols.vcs import PlanStep

AreaName = Literal["candidate", "control", "publication"]
PLACEHOLDER_NAMES: Final[tuple[str, ...]] = (
    "candidate_root",
    "control_root",
    "publication_root",
    "input_location",
    "base_commit",
    "commit",
    "branch",
)
PRIVATE_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"control_root", "publication_root", "input_location"}
)
TEXT_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    "\\{(" + "|".join(PLACEHOLDER_NAMES) + ")\\}"
)
BYTES_PLACEHOLDER: Final[re.Pattern[bytes]] = re.compile(
    TEXT_PLACEHOLDER.pattern.encode("ascii")
)
MAX_STDERR_KEPT: Final[int] = 65_536
DEFAULT_FAILURE: Final[dict[Phase, ErrorClass]] = {
    Phase.CHECKOUT: ErrorClass.INFRASTRUCTURE,
    Phase.EXPORT: ErrorClass.ARTIFACT_INVALID,
    Phase.PREPARE: ErrorClass.INFRASTRUCTURE,
    Phase.INSPECT: ErrorClass.ARTIFACT_INVALID,
    Phase.PUBLISH: ErrorClass.INFRASTRUCTURE,
}
PHASE_AREAS: Final[dict[Phase, frozenset[AreaName]]] = {
    Phase.CHECKOUT: frozenset({"candidate", "control"}),
    Phase.EXPORT: frozenset({"candidate"}),
    Phase.PREPARE: frozenset({"control", "publication"}),
    Phase.INSPECT: frozenset({"control", "publication"}),
    Phase.PUBLISH: frozenset({"control", "publication"}),
}
REDACTED: Final[str] = "[redacted]"


class ToolMissing(LookupError):
    def __init__(self) -> None:
        super().__init__("a required tool is not installed")


@dataclass(frozen=True)
class Areas:
    candidate: Path | None
    control: Path | None
    publication: Path | None

    def path(self, name: AreaName) -> Path | None:
        if name == "candidate":
            return self.candidate
        if name == "control":
            return self.control
        return self.publication


def resolve_tool(name: str, search_path: Sequence[str]) -> str:
    if "/" in name or not name:
        raise ToolMissing
    for entry in search_path:
        if not entry.startswith("/"):
            continue
        candidate = Path(entry) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise ToolMissing


def resolve_tools(names: Iterable[str], search_path: Sequence[str]) -> dict[str, str]:
    return {name: resolve_tool(name, search_path) for name in names}


def selected_tools(
    required: Sequence[str], resolved: Mapping[str, str], phase: Phase
) -> dict[str, str]:
    if any(name not in resolved for name in required):
        misconfigured("tool_missing", phase)
    return {name: resolved[name] for name in required}


@dataclass(frozen=True)
class StepContext:
    phase: Phase
    areas: Areas
    values: Mapping[str, str]
    tools: Mapping[str, str]
    launcher: StepLauncher
    guard: PhaseGuard
    logger: ComponentLogger
    redactions: tuple[str, ...] = ()


def placeholder_value(name: str, context: StepContext) -> str:
    if context.phase is Phase.EXPORT and name in PRIVATE_PLACEHOLDERS:
        misconfigured("placeholder_refused", context.phase)
    value = context.values.get(name)
    if value is None:
        misconfigured("placeholder_unavailable", context.phase)
    return value


def substituted(text: str, context: StepContext) -> str:
    return TEXT_PLACEHOLDER.sub(
        lambda match: placeholder_value(match[1], context), text
    )


def substituted_bytes(data: bytes, context: StepContext) -> bytes:
    return BYTES_PLACEHOLDER.sub(
        lambda match: placeholder_value(match[1].decode("ascii"), context).encode(
            "utf-8"
        ),
        data,
    )


def step_argv(step: PlanStep, context: StepContext) -> tuple[str, ...]:
    tool = context.tools.get(step.argv[0])
    if tool is None:
        misconfigured("tool_not_required", context.phase)
    return (tool, *(substituted(argument, context) for argument in step.argv[1:]))


def step_cwd(step: PlanStep, context: StepContext) -> Path:
    if step.cwd not in PHASE_AREAS[context.phase]:
        misconfigured("step_area_refused", context.phase)
    path = context.areas.path(step.cwd)
    if path is None:
        misconfigured("step_area_missing", context.phase)
    return path


def launch_spec(step: PlanStep, context: StepContext) -> LaunchSpec:
    argv = step_argv(step, context)
    cwd = step_cwd(step, context)
    env = {name: substituted(value, context) for name, value in step.env.items()}
    stdin = None if step.stdin is None else substituted_bytes(step.stdin, context)
    timeout_ns = context.guard.bound_ns(duration_ns(step.timeout), context.phase)
    return LaunchSpec(
        argv=argv,
        cwd=cwd,
        env=env,
        stdin=stdin,
        timeout_ns=timeout_ns,
        stdout_limit=capture_limit(step.capture),
        stderr_limit=MAX_STDERR_KEPT,
    )


def failure_class(step: PlanStep, phase: Phase) -> ErrorClass:
    if step.failure_class is None:
        return DEFAULT_FAILURE[phase]
    return ErrorClass(step.failure_class)


def report_failure(
    context: StepContext,
    index: int,
    error: ExecutorError,
    outcome: StepOutcome | None,
) -> None:
    fields: dict[str, object] = {
        "phase": context.phase.value,
        "step": index,
        "failure_class": error.failure_class.value,
        "code": error.code,
    }
    if outcome is not None:
        fields["stderr_dropped"] = outcome.stderr_dropped
        if outcome.exit_code is not None:
            fields["exit_code"] = outcome.exit_code
    context.logger.emit(STEP_FAILED, **fields)


def outcome_error(
    step: PlanStep, index: int, outcome: StepOutcome, phase: Phase
) -> ExecutorError | None:
    position = StepPosition(phase, index, outcome.exit_code)
    if outcome.ending is Ending.UNKNOWN:
        return ExecutorError(
            ErrorClass.INFRASTRUCTURE, "step_outcome_unknown", phase, position
        )
    if outcome.ending is Ending.TIMED_OUT:
        return ExecutorError(ErrorClass.TIMEOUT, "step_timed_out", phase, position)
    if outcome.exit_code != 0:
        return ExecutorError(failure_class(step, phase), "step_failed", phase, position)
    return None


async def launched(step: PlanStep, index: int, context: StepContext) -> StepOutcome:
    spec = launch_spec(step, context)
    try:
        return await context.launcher.run(spec)
    except OSError:
        error = ExecutorError(
            ErrorClass.INFRASTRUCTURE,
            "step_spawn_failed",
            context.phase,
            StepPosition(context.phase, index, None),
        )
        report_failure(context, index, error, None)
        raise error from None


async def run_step(
    step: PlanStep, index: int, context: StepContext, captures: Captures
) -> tuple[Captures, StepOutcome]:
    outcome = await launched(step, index, context)
    error = outcome_error(step, index, outcome, context.phase)
    if error is not None:
        report_failure(context, index, error, outcome)
        raise error
    context.logger.emit(STEP_FINISHED, phase=context.phase.value, step=index)
    if step.capture is None:
        return captures, outcome
    try:
        return (
            parsed(
                captures,
                step.capture,
                outcome.stdout,
                outcome.stdout_overflow,
                context.phase,
            ),
            outcome,
        )
    except ExecutorError as refused:
        report_failure(context, index, refused, outcome)
        raise


async def run_steps(
    steps: Sequence[PlanStep], context: StepContext, first_index: int = 0
) -> Captures:
    captures = Captures()
    for offset, step in enumerate(steps):
        captures, _ = await run_step(step, first_index + offset, context, captures)
    return captures


def redacted(text: str, values: Sequence[str]) -> str:
    for value in sorted(values, key=len, reverse=True):
        if value:
            text = text.replace(value, REDACTED)
    return text
