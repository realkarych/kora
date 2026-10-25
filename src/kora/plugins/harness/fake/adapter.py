import asyncio
import os
import stat
from contextlib import aclosing
from dataclasses import dataclass
from typing import Final, Literal, cast

from kora.ir import ConfigWarning, ErrorClass
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.harness.fake.script import (
    BoundedSteps,
    ChangeStep,
    EventStep,
    HarnessDenialStep,
    OutputStep,
    PauseStep,
    ResultStep,
    ScriptRefused,
    ScriptSource,
    Step,
    ToolCallStep,
    UndecidedToolResultStep,
    UsageStep,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    MAX_SAMPLE_ITEMS,
    DenialSample,
    ErrorData,
    EventSink,
    EventSummary,
    HarnessCapabilities,
    LaunchContext,
    OutputCandidate,
    PermissionDeniedData,
    ProviderLimitedData,
    ProviderRetryingData,
    ResultData,
    RunnerCause,
    RunnerOutcome,
    SessionResult,
    SessionSpec,
    ToolCallData,
    ToolResultData,
    TurnData,
)
from kora.protocols.policy import (
    RULE_ID,
    PolicyClient,
    PolicyDecision,
    PolicyLayers,
    PolicyValueRefused,
    ToolCallRequest,
)
from kora.protocols.vcs import WorkspaceRef, WorkspaceTarget
from kora.runner.calls import (
    ToolCallLedger,
    closed_exception,
    decide_call,
    refused_call,
)
from kora.runner.outcome import final_class, select_output
from kora.runner.usage import UsageLedger

NAME: Final[str] = "fake"
VERSION: Final[str] = "fake-1"
SESSION_ID: Final[str] = "fake-session"
COMPONENT: Final[str] = CATALOG.register_component("harness.fake")
SESSION_FINISHED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "harness.fake.session_finished",
        COMPONENT,
        Level.INFO,
        "a scripted fake session finished",
        {
            "cause": FieldKind.STRING,
            "termination": FieldKind.STRING,
            "steps": FieldKind.INTEGER,
        },
    )
)
SCRIPT_REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "harness.fake.script_refused",
        COMPONENT,
        Level.WARNING,
        "a fake harness script was refused",
        {"reason": FieldKind.STRING},
    )
)
EVENT_DATA: Final[
    dict[
        str,
        type[TurnData]
        | type[ProviderLimitedData]
        | type[ProviderRetryingData]
        | type[ErrorData],
    ]
] = {
    "turn": TurnData,
    "provider_limited": ProviderLimitedData,
    "provider_retrying": ProviderRetryingData,
    "error": ErrorData,
}
Termination = Literal["none", "interrupted", "killed_after_grace"]


@dataclass(frozen=True)
class CallScope:
    harness: str
    workspace: WorkspaceRef
    node_path: str
    layers: PolicyLayers
    protected_paths: tuple[str, ...]
    output_format: bool


@dataclass(frozen=True)
class Stop:
    cause: RunnerCause
    interrupt: bool


class FakeSummary(EventSummary):
    def __init__(self, harness: str) -> None:
        self.harness = harness
        self.calls = ToolCallLedger()
        self.usage = UsageLedger()
        self.denials: list[PermissionDeniedData] = []
        self.denial_total = 0
        self.turns = 0
        self.result: Literal["success", "error"] | None = None
        self.provider_limited = False
        self.structured: OutputCandidate | None = None
        self.result_text: OutputCandidate | None = None
        self.steps = 0

    def deny(self, denial: PermissionDeniedData) -> None:
        self.denial_total += 1
        if len(self.denials) < MAX_SAMPLE_ITEMS:
            self.denials.append(denial)

    def denial_sample(self) -> DenialSample:
        return DenialSample(items=tuple(self.denials), total=self.denial_total)


def denial_fields(decision: PolicyDecision) -> tuple[str | None, str]:
    if RULE_ID.fullmatch(decision.reason) is not None:
        return decision.reason, "denied"
    return None, decision.reason


def path_refused() -> ScriptRefused:
    return ScriptRefused("script_change_path_invalid", "a changed path is invalid")


def write_change(root: str, step: ChangeStep) -> None:
    for path, content in step.files.items():
        parts = path.split("/")
        if any(part in {"", ".", ".."} or "\0" in part for part in parts):
            raise path_refused()
        try:
            write_file(root, parts, content.encode("utf-8"))
        except OSError:
            raise path_refused() from None


def write_file(root: str, parts: list[str], content: bytes) -> None:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        target = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o644,
            dir_fd=directory,
        )
        with os.fdopen(target, "wb") as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise path_refused()
            os.ftruncate(handle.fileno(), 0)
            handle.write(content)
    finally:
        os.close(directory)


class FakeHarness:
    name: str = NAME
    config_schema: JsonObject = {"type": "object", "additionalProperties": False}
    egress_hosts: tuple[str, ...] = ()
    tested_versions: str = VERSION
    reserved_env: tuple[str, ...] = ()

    def __init__(
        self, source: ScriptSource, scope: CallScope, ctx: ProtocolContext
    ) -> None:
        self.source = source
        self.scope = scope
        self.ctx = ctx
        self.killed = asyncio.Event()

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            resume=False,
            reports_usage_per_turn=True,
            policy_coverage=frozenset({"shell", "file_edit", "mcp_tool"}),
        )

    def check_config(self, config: JsonObject) -> list[ConfigWarning]:
        return []

    def build_session_spec(
        self, config: JsonObject, workspace: WorkspaceTarget, feedback: None
    ) -> SessionSpec:
        return SessionSpec.model_validate(
            {
                "harness": NAME,
                "adapter_version": VERSION,
                "options": {},
                "files": (),
                "prompt": "",
                "resume_handle": None,
                "price_table_id": NAME,
                "price_table": {},
            }
        )

    def installed_version(self) -> str:
        return VERSION

    def kill(self) -> None:
        self.killed.set()

    async def run(
        self,
        spec: SessionSpec,
        policy: PolicyClient,
        sink: EventSink,
        launches: LaunchContext,
    ) -> SessionResult:
        summary = FakeSummary(spec.harness)
        self.killed = asyncio.Event()
        play = asyncio.create_task(self.play(summary, policy, sink))
        stopped = asyncio.create_task(sink.termination())
        try:
            done, _ = await asyncio.wait(
                {play, stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            if play in done:
                stop = play.result()
            else:
                stop = Stop(stopped.result(), True)
            termination: Termination = "none"
            if stop.interrupt:
                termination = await self.interrupted(summary, sink, play)
        finally:
            await cancelled(
                play,
                stopped,
            )
        self.ctx.logger.emit(
            SESSION_FINISHED,
            cause=stop.cause,
            termination=termination,
            steps=summary.steps,
        )
        return self.finalise(
            summary, RunnerOutcome(stop.cause, termination, None, None)
        )

    async def interrupted(
        self, summary: FakeSummary, sink: EventSink, play: "asyncio.Task[Stop]"
    ) -> Termination:
        if self.source.header.on_interrupt == "acknowledge":
            await cancelled(play)
            if summary.result is None:
                emit_result(summary, sink, "error")
            return "interrupted"
        await self.killed.wait()
        return "killed_after_grace"

    async def play(
        self, summary: FakeSummary, policy: PolicyClient, sink: EventSink
    ) -> Stop:
        try:
            async with aclosing(BoundedSteps(self.source).steps()) as steps:
                async for step in steps:
                    summary.steps += 1
                    stop = await self.act(step, summary, policy, sink)
                    if stop is not None:
                        return stop
                    cause = sink.enforcement().cause
                    if cause is not None and cause != "max_events":
                        return Stop(cause, True)
        except ScriptRefused as error:
            self.ctx.logger.emit(SCRIPT_REFUSED, reason=error.code)
            raise
        if exhausted(sink):
            return Stop("max_events", True)
        return Stop(self.source.header.end, False)

    async def act(
        self, step: Step, summary: FakeSummary, policy: PolicyClient, sink: EventSink
    ) -> Stop | None:
        match step:
            case EventStep():
                emit_event(step, summary, sink)
            case ToolCallStep():
                return await self.call(step, summary, policy, sink)
            case UndecidedToolResultStep():
                emit_tool_result(sink, step.tool_use_id, step.is_error, None)
                if summary.calls.result(step.tool, step.tool_use_id, step.is_error):
                    return Stop("policy_bypass", False)
            case HarnessDenialStep():
                harness_denial(
                    summary, sink, step.tool, step.tool_use_id, "harness_rule"
                )
            case UsageStep():
                sink.emit("usage", step.data.model_dump(mode="json"))
                summary.usage.observe(step.data)
                if step.closes:
                    summary.usage.close(step.data.message_id)
            case OutputStep():
                record_output(summary, step)
            case ChangeStep():
                write_change(self.scope.workspace.root, step)
            case PauseStep():
                await asyncio.Event().wait()
            case ResultStep():
                emit_result(summary, sink, step.status)
        return None

    async def call(
        self,
        step: ToolCallStep,
        summary: FakeSummary,
        policy: PolicyClient,
        sink: EventSink,
    ) -> Stop | None:
        scope = self.scope
        request: ToolCallRequest | None
        try:
            request = ToolCallRequest(
                harness=scope.harness,
                tool=step.tool,
                tool_use_id=step.tool_use_id,
                agent_id=step.agent_id,
                arguments=cast("JsonObject", step.input_shape),
                workspace=scope.workspace,
                node_path=scope.node_path,
            )
        except PolicyValueRefused:
            request = None
        if request is None:
            verdict = closed_exception(
                summary.calls,
                sink.enforcement(),
                step.tool,
                step.tool_use_id,
                step.agent_id,
                scope.output_format,
            ) or refused_call(summary.calls, step.tool_use_id)
        else:
            verdict = await decide_call(
                summary.calls,
                sink.enforcement(),
                policy,
                request,
                scope.layers,
                scope.protected_paths,
                scope.output_format,
            )
        if not verdict.counted:
            return None
        call = ToolCallData(
            tool=step.tool,
            tool_use_id=step.tool_use_id,
            agent_id=step.agent_id,
            output_channel=verdict.output_channel,
        )
        sink.emit("tool_call", call.model_dump(mode="json", exclude_none=True))
        if request is None and not verdict.allow:
            policy_denial(summary, sink, step, None)
            return Stop("policy_error", False)
        if verdict.cause is not None:
            harness_denial(summary, sink, step.tool, step.tool_use_id, verdict.cause)
            return Stop(verdict.cause, False)
        if not verdict.allow:
            policy_denial(summary, sink, step, verdict.decision)
            return None
        if step.outcome is not None and not exhausted(sink):
            emit_tool_result(
                sink, step.tool_use_id, step.outcome.is_error, step.outcome.content
            )
        return None

    def finalise(self, summary: EventSummary, outcome: RunnerOutcome) -> SessionResult:
        if not isinstance(summary, FakeSummary):
            raise KoraProtocolError(
                "summary_foreign", "the summary belongs to another adapter", False, {}
            )
        result_seen = summary.result is not None
        denials = summary.denial_sample()
        bypass = summary.calls.bypass(denials, result_seen)
        result_class = ErrorClass.HARNESS_ERROR if summary.result == "error" else None
        completed = outcome.cause == "completed"
        return SessionResult(
            harness=summary.harness,
            session_id=SESSION_ID,
            installed_version=VERSION,
            billing_class=None,
            stop_reason=outcome.cause,
            terminal_reason=None,
            api_error_status=None,
            error_class=final_class(
                outcome.cause, result_class, bypass, summary.provider_limited
            ),
            error_summary=None,
            output=select_output(summary.structured, summary.result_text),
            output_diagnostic=None,
            usage=summary.usage.rows(),
            usage_coverage=summary.usage.coverage(result_seen and completed),
            turns=summary.turns,
            denials=denials,
            denials_complete=denials.total == len(denials.items)
            and bypass.unconfirmed.total == 0,
            unconfirmed_calls=bypass.unconfirmed,
            policy_bypass=bypass.confirmed,
        )


async def cancelled(
    *tasks: "asyncio.Task[Stop] | asyncio.Task[RunnerCause]",
) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.wait(tasks)
    current = asyncio.current_task()
    if current is not None and current.cancelling():
        raise asyncio.CancelledError


def exhausted(sink: EventSink) -> bool:
    return sink.enforcement().cause == "max_events"


def emit_event(step: EventStep, summary: FakeSummary, sink: EventSink) -> None:
    data = EVENT_DATA[step.event].model_validate(dict(step.data))
    if step.event == "turn" and exhausted(sink):
        return
    if step.event == "turn":
        summary.turns += 1
    if step.event == "provider_limited":
        summary.provider_limited = True
    sink.emit(step.event, data.model_dump(mode="json", exclude_none=True))


def emit_tool_result(
    sink: EventSink, tool_use_id: str, is_error: bool, content: str | None
) -> None:
    data = ToolResultData(
        tool_use_id=tool_use_id, is_error=is_error, content=content, truncated=False
    )
    sink.emit("tool_result", data.model_dump(mode="json", exclude_none=True))


def emit_result(
    summary: FakeSummary, sink: EventSink, status: Literal["success", "error"]
) -> None:
    summary.result = status
    data = ResultData(subtype=status, is_error=status == "error", terminal_reason=None)
    sink.emit("result", data.model_dump(mode="json"))


def harness_denial(
    summary: FakeSummary,
    sink: EventSink,
    tool: str,
    tool_use_id: str,
    code: Literal["harness_rule", "policy_error", "policy_timeout"],
) -> None:
    denial = PermissionDeniedData(
        tool=tool, tool_use_id=tool_use_id, source="harness", code=code
    )
    summary.deny(denial)
    sink.emit("permission_denied", denial.model_dump(mode="json", exclude_none=True))


def policy_denial(
    summary: FakeSummary,
    sink: EventSink,
    step: ToolCallStep,
    decision: PolicyDecision | None,
) -> None:
    if decision is None:
        pattern, code = None, "request_invalid"
    else:
        pattern, code = denial_fields(decision)
    denial = PermissionDeniedData.model_validate(
        {
            "tool": step.tool,
            "tool_use_id": step.tool_use_id,
            "source": "policy",
            "pattern": pattern,
            "code": code,
        }
    )
    summary.deny(denial)
    sink.emit("permission_denied", denial.model_dump(mode="json", exclude_none=True))


def record_output(summary: FakeSummary, step: OutputStep) -> None:
    candidate = OutputCandidate(source=step.source, value=step.value)
    if step.source == "structured_output":
        summary.structured = candidate
    else:
        summary.result_text = candidate
