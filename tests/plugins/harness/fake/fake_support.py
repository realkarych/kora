from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Final, Literal

from kora.conformance.harness import (
    ConformanceSink,
    HarnessCase,
    HarnessHost,
    NoLaunches,
    Trace,
)
from kora.ir import ToolPolicy
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.harness.fake import CallScope, FakeHarness, ListedScript, ScriptHeader
from kora.plugins.harness.fake.script import STEP_ADAPTER, Step
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import (
    Enforcement,
    NormalisedEvent,
    RunnerCause,
    SessionResult,
    UsageData,
)
from kora.protocols.policy import (
    PolicyClient,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.protocols.vcs import WorkspaceRef, WorkspaceTarget

DENIED_TOOL: Final[str] = "Forbidden"
OBSERVED_AT: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)


def step(document: JsonObject) -> Step:
    return STEP_ADAPTER.validate_python(document)


def header(
    end: Literal["completed", "cli_exit_without_result", "oversized_message"],
    on_interrupt: Literal["acknowledge", "ignore"] = "acknowledge",
) -> ScriptHeader:
    return ScriptHeader(end=end, on_interrupt=on_interrupt)


def turn(message_id: str, text: str | None = None) -> JsonObject:
    data: JsonObject = {"message_id": message_id, "truncated": False}
    if text is not None:
        data["text"] = text
    return {"kind": "event", "event": "turn", "data": data}


def tool_call(
    tool: str,
    tool_use_id: str,
    outcome: bool | None = False,
    agent_id: str | None = None,
    arguments: JsonValue = None,
) -> JsonObject:
    return {
        "kind": "tool_call",
        "tool": tool,
        "tool_use_id": tool_use_id,
        "agent_id": agent_id,
        "input_shape": {"command": "true"} if arguments is None else arguments,
        "outcome": (
            None if outcome is None else {"is_error": outcome, "content": "done"}
        ),
    }


def result(status: Literal["success", "error"] = "success") -> JsonObject:
    return {"kind": "result", "status": status}


def observation(
    message_id: str,
    sequence: int,
    model: str | None = "fake-model",
    completeness: Literal["complete", "partial", "unavailable"] = "complete",
    output_tokens: int = 7,
) -> UsageData:
    return UsageData.model_validate(
        {
            "observation_id": f"observation-{sequence}",
            "observation_sequence": sequence,
            "observed_at": OBSERVED_AT,
            "message_id": message_id,
            "model": model,
            "input_tokens": 11,
            "cached_input_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": output_tokens,
            "reasoning_tokens": 0,
            "known_token_fields": [
                "input_tokens",
                "cached_input_tokens",
                "cache_creation_tokens",
                "output_tokens",
                "reasoning_tokens",
            ],
            "basis": "price_table",
            "billing_class": "api_key",
            "coverage": {
                "version": 1,
                "observation_id": f"coverage-{sequence}",
                "observation_sequence": sequence,
                "completeness": completeness,
                "observed_at": OBSERVED_AT,
                "attribution_gap": False,
                "observation_gap": False,
            },
        }
    )


def usage(message_id: str, sequence: int, closes: bool = True) -> JsonObject:
    data = observation(message_id, sequence)
    return {"kind": "usage", "data": data.model_dump(mode="json"), "closes": closes}


def scripted(
    documents: Sequence[JsonObject],
    end: Literal["completed", "cli_exit_without_result", "oversized_message"] = (
        "completed"
    ),
    on_interrupt: Literal["acknowledge", "ignore"] = "acknowledge",
) -> ListedScript:
    return ListedScript(
        header(end, on_interrupt), tuple(step(document) for document in documents)
    )


def workspace(root: str) -> WorkspaceRef:
    return WorkspaceRef(
        attempt_id="attempt-1",
        root=root,
        repo="example/repo",
        commit="c0ffee",
        base_commit="c0ffee",
        branch=None,
    )


def layers(deny: tuple[str, ...] = ()) -> PolicyLayers:
    empty = ToolPolicy.model_validate({"allow": [], "deny": []})
    node = ToolPolicy.model_validate({"allow": [], "deny": list(deny)})
    return PolicyLayers(namespace=empty, node=node)


def scope(root: str = "/kora-fake-workspace", deny: tuple[str, ...] = ()) -> CallScope:
    return CallScope(
        harness="claude",
        workspace=workspace(root),
        node_path="graph.node",
        layers=layers(deny),
        protected_paths=(),
        output_format=True,
    )


def harness(
    script: ListedScript, ctx: ProtocolContext, root: str = "/kora-fake-workspace"
) -> FakeHarness:
    return FakeHarness(script, scope(root), ctx)


class AllowingPolicy:
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        return PolicyDecision(allow=True, reason="allowed")


class ScriptedSink(ConformanceSink):
    def __init__(
        self,
        max_events: int | None = None,
        on_emit: Callable[[NormalisedEvent], None] | None = None,
    ) -> None:
        super().__init__(max_events, Trace())
        self.forced: RunnerCause | None = None
        self.on_emit = on_emit

    def emit(self, kind: str, data: JsonObject) -> None:
        super().emit(kind, data)
        if self.on_emit is not None:
            self.on_emit(self.events[-1])

    def enforcement(self) -> Enforcement:
        if self.forced is not None:
            return Enforcement("terminate", self.forced)
        return super().enforcement()

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


async def played(
    adapter: FakeHarness, sink: ScriptedSink, policy: PolicyClient | None = None
) -> SessionResult:
    spec = adapter.build_session_spec(
        {}, WorkspaceTarget(root=adapter.scope.workspace.root, host_path=None), None
    )
    return await adapter.run(spec, policy or AllowingPolicy(), sink, NoLaunches())


class FakeExercise:
    denied_tool: str = DENIED_TOOL

    async def failure(self, ctx: ProtocolContext, marker: str) -> object:
        script = scripted([turn("m1", marker)])
        return await HarnessHost().hosted(harness(script, ctx), DENIED_TOOL, None)

    def adapter(self, case: HarnessCase, ctx: ProtocolContext) -> FakeHarness:
        return harness(CASES[case], ctx)


def structured_call() -> JsonObject:
    return tool_call("StructuredOutput", "call-output", outcome=None)


def output(source: Literal["structured_output", "result_text"]) -> JsonObject:
    return {"kind": "output", "source": source, "value": {"answer": 42}}


CASES: Final[dict[HarnessCase, ListedScript]] = {
    HarnessCase.TOOL_CALLS: scripted(
        [
            turn("m1"),
            tool_call("Bash", "call-1"),
            tool_call(DENIED_TOOL, "call-2"),
            result(),
        ]
    ),
    HarnessCase.MAX_EVENTS: scripted(
        [
            turn("m1"),
            turn("m2"),
            tool_call("Bash", "call-1"),
            tool_call("Bash", "call-2"),
            turn("m3"),
            result(),
        ]
    ),
    HarnessCase.USAGE: scripted([turn("m1"), usage("m1", 1), result()]),
    HarnessCase.NO_USAGE: scripted([turn("m1"), result()]),
    HarnessCase.STRUCTURED_OUTPUT: scripted(
        [turn("m1"), structured_call(), output("structured_output"), result()]
    ),
    HarnessCase.RESULT_TEXT: scripted([turn("m1"), output("result_text"), result()]),
}
