import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import (
    check,
    context_for,
    fail,
    kora_capture,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.ir import ErrorClass
from kora.ir.json import JsonObject
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EVENT_ADAPTER,
    Enforcement,
    HarnessAdapter,
    Launch,
    LaunchInputs,
    NormalisedEvent,
    Placement,
    RunnerCause,
    SessionResult,
)
from kora.protocols.policy import (
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.protocols.vcs import WorkspaceTarget
from kora.runner.calls import LIMITED_KINDS, EventCount

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_harness_host",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_harness_events_are_normalised",
    "test_harness_max_events_denies_at_exact_count",
    "test_harness_policy_precedes_every_tool_call",
    "test_harness_structured_output_and_fallbacks",
    "test_harness_usage_completeness",
    "test_provider_logs_through_its_context",
]

EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "session_started",
        "turn",
        "tool_call",
        "tool_result",
        "permission_denied",
        "usage",
        "provider_limited",
        "provider_retrying",
        "result",
        "error",
    }
)
EVENT_TIME: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)
CONFORMANCE_WORKSPACE: Final[str] = "/kora-conformance-workspace"
MAX_EVENTS: Final[int] = 3


class HarnessCase(StrEnum):
    TOOL_CALLS = "tool_calls"
    MAX_EVENTS = "max_events"
    USAGE = "usage"
    NO_USAGE = "no_usage"
    STRUCTURED_OUTPUT = "structured_output"
    RESULT_TEXT = "result_text"


class HarnessExercise(Protocol):
    denied_tool: str

    def failure(self, ctx: ProtocolContext, marker: str) -> Awaitable[object]: ...

    def adapter(self, case: HarnessCase, ctx: ProtocolContext) -> HarnessAdapter: ...


@dataclass
class Trace:
    entries: list[tuple[str, str]] = field(default_factory=lambda: [])


class ConformanceSink:
    def __init__(self, max_events: int | None, trace: Trace) -> None:
        self.count = EventCount(max_events)
        self.trace = trace
        self.events: list[NormalisedEvent] = []
        self.stopped: asyncio.Future[RunnerCause] | None = None

    def emit(self, kind: str, data: JsonObject) -> None:
        if kind not in EVENT_KINDS:
            raise KoraProtocolError(
                "event_kind_refused", "the event kind is not accepted", False, {}
            )
        event = EVENT_ADAPTER.validate_python(
            {"i": len(self.events), "at": EVENT_TIME, "kind": kind, "data": data}
        )
        self.events.append(event)
        self.trace.entries.append((kind, tool_use_id_of(event)))
        self.count.count(kind)

    def observe_dropped(self, kind: str) -> None:
        return None

    def enforcement(self) -> Enforcement:
        return self.count.enforcement()

    def future(self) -> "asyncio.Future[RunnerCause]":
        if self.stopped is None:
            self.stopped = asyncio.get_running_loop().create_future()
        return self.stopped

    async def termination(self) -> RunnerCause:
        return await asyncio.shield(self.future())

    def stop(self, cause: RunnerCause) -> None:
        self.future().set_result(cause)


def tool_use_id_of(event: NormalisedEvent) -> str:
    return str(getattr(event.data, "tool_use_id", ""))


class ConformancePolicy:
    def __init__(self, denied_tool: str, trace: Trace) -> None:
        self.denied_tool = denied_tool
        self.trace = trace

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.trace.entries.append(("decide", request.tool_use_id))
        if request.tool == self.denied_tool:
            return PolicyDecision(allow=False, reason="not_allowed")
        return PolicyDecision(allow=True, reason="allowed")


class NoLaunches:
    placements: Mapping[Placement, Path] = {}

    async def allocate(self, inputs: LaunchInputs) -> Launch:
        raise KoraProtocolError(
            "launch_unavailable", "the conformance host launches nothing", False, {}
        )


@dataclass(frozen=True)
class HostedRun:
    result: SessionResult
    events: tuple[NormalisedEvent, ...]
    trace: tuple[tuple[str, str], ...]


class HarnessHost:
    async def hosted(
        self, adapter: HarnessAdapter, denied_tool: str, max_events: int | None
    ) -> HostedRun:
        trace = Trace()
        sink = ConformanceSink(max_events, trace)
        policy = ConformancePolicy(denied_tool, trace)
        spec = adapter.build_session_spec(
            {}, WorkspaceTarget(root=CONFORMANCE_WORKSPACE, host_path=None), None
        )
        result = await adapter.run(spec, policy, sink, NoLaunches())
        return HostedRun(result, tuple(sink.events), tuple(trace.entries))

    def run(
        self, adapter: HarnessAdapter, denied_tool: str, max_events: int | None
    ) -> HostedRun:
        return asyncio.run(self.hosted(adapter, denied_tool, max_events))


@pytest.fixture
def kora_provider(kora_harness_provider: HarnessAdapter) -> HarnessAdapter:
    return kora_harness_provider


@pytest.fixture
def kora_exercise(kora_harness_exercise: HarnessExercise) -> object:
    return kora_harness_exercise.failure


@pytest.fixture
def kora_harness_host() -> HarnessHost:
    return HarnessHost()


def hosted(
    host: HarnessHost,
    exercise: HarnessExercise,
    capture: Capture,
    case: HarnessCase,
    max_events: int | None = None,
) -> HostedRun:
    adapter = exercise.adapter(case, context_for(capture))
    return host.run(adapter, exercise.denied_tool, max_events)


@pytest.mark.kora_conformance
def test_harness_events_are_normalised(
    kora_harness_exercise: HarnessExercise,
    kora_harness_host: HarnessHost,
    kora_capture: Capture,
) -> None:
    run = hosted(
        kora_harness_host, kora_harness_exercise, kora_capture, HarnessCase.TOOL_CALLS
    )

    check(bool(run.events), "the session emits normalised events")
    check(
        all(event.kind in EVENT_KINDS for event in run.events),
        "every event is one of the ten normalised kinds",
    )
    check(
        any(event.kind == "result" for event in run.events),
        "a completed session emits its result",
    )
    check(run.result.error_class is None, "a completed session has no error class")


@pytest.mark.kora_conformance
def test_harness_policy_precedes_every_tool_call(
    kora_harness_exercise: HarnessExercise,
    kora_harness_host: HarnessHost,
    kora_capture: Capture,
) -> None:
    run = hosted(
        kora_harness_host, kora_harness_exercise, kora_capture, HarnessCase.TOOL_CALLS
    )

    decided = [identifier for kind, identifier in run.trace if kind == "decide"]
    check(len(decided) >= 2, "the session asks the policy for its tool calls")
    for position, (kind, identifier) in enumerate(run.trace):
        if kind == "tool_result":
            check(
                ("decide", identifier) in run.trace[:position],
                "the policy decides a call before its result",
            )
    denied = [
        event
        for event in run.events
        if event.kind == "permission_denied" and event.data.source == "policy"
    ]
    check(bool(denied), "a call the policy denies is reported as denied")
    refused = {tool_use_id_of(event) for event in denied}
    check(
        not any(
            event.kind == "tool_result" and tool_use_id_of(event) in refused
            for event in run.events
        ),
        "a denied call has no result",
    )
    check(not run.result.policy_bypass, "no call bypasses the policy")


@pytest.mark.kora_conformance
def test_harness_max_events_denies_at_exact_count(
    kora_harness_exercise: HarnessExercise,
    kora_harness_host: HarnessHost,
    kora_capture: Capture,
) -> None:
    run = hosted(
        kora_harness_host,
        kora_harness_exercise,
        kora_capture,
        HarnessCase.MAX_EVENTS,
        MAX_EVENTS,
    )

    limited = [event for event in run.events if event.kind in LIMITED_KINDS]
    check(len(limited) == MAX_EVENTS, "exactly max_events limited events are emitted")
    decisions_after = [
        kind
        for kind, _ in run.trace[trace_index_of_limit(run.trace) + 1 :]
        if kind == "decide"
    ]
    check(not decisions_after, "no call reaches the policy after the limit")
    check(
        run.result.error_class is ErrorClass.EVENTS_EXHAUSTED,
        "the session ends with events_exhausted",
    )


def trace_index_of_limit(trace: tuple[tuple[str, str], ...]) -> int:
    seen = 0
    for index, (kind, _) in enumerate(trace):
        if kind in LIMITED_KINDS:
            seen += 1
            if seen == MAX_EVENTS:
                return index
    fail("the session reaches max_events")


@pytest.mark.kora_conformance
def test_harness_usage_completeness(
    kora_harness_exercise: HarnessExercise,
    kora_harness_host: HarnessHost,
    kora_capture: Capture,
) -> None:
    reported = hosted(
        kora_harness_host, kora_harness_exercise, kora_capture, HarnessCase.USAGE
    )
    silent = hosted(
        kora_harness_host, kora_harness_exercise, kora_capture, HarnessCase.NO_USAGE
    )

    check(bool(reported.result.usage), "reported usage yields model rows")
    check(
        reported.result.usage_coverage.completeness == "complete",
        "complete final usage is complete",
    )
    check(not silent.result.usage, "no usage yields no model rows")
    check(
        silent.result.usage_coverage.completeness == "unavailable",
        "no usage is unavailable",
    )


@pytest.mark.kora_conformance
def test_harness_structured_output_and_fallbacks(
    kora_harness_exercise: HarnessExercise,
    kora_harness_host: HarnessHost,
    kora_capture: Capture,
) -> None:
    structured = hosted(
        kora_harness_host,
        kora_harness_exercise,
        kora_capture,
        HarnessCase.STRUCTURED_OUTPUT,
    )
    text = hosted(
        kora_harness_host, kora_harness_exercise, kora_capture, HarnessCase.RESULT_TEXT
    )

    check(
        structured.result.output is not None
        and structured.result.output.source == "structured_output",
        "the structured output channel is selected",
    )
    check(
        any(
            event.kind == "tool_call" and event.data.output_channel
            for event in structured.events
        ),
        "the output channel call is recorded",
    )
    check(
        text.result.output is not None and text.result.output.source == "result_text",
        "result text is the fallback",
    )
