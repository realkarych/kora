import asyncio
from dataclasses import dataclass, field
from typing import Final, cast

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import ErrorClass
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.harness.fake import CallScope, FakeHarness
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import PermissionDeniedData
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from tests.plugins.harness.fake.fake_support import (
    ScriptedSink,
    layers,
    played,
    result,
    scripted,
    tool_call,
    turn,
    workspace,
)

DENIED_COMMAND: Final[str] = "rm -rf build"


@dataclass
class AllowDenyClient:
    ctx: ProtocolContext
    provider: AllowDenyPolicy = field(default_factory=AllowDenyPolicy)
    asked: list[str] = field(default_factory=lambda: [])

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.asked.append(request.tool_use_id)
        return await self.provider.decide(request, layers, protected_paths, self.ctx)


@dataclass
class MalformedClient:
    asked: list[str] = field(default_factory=lambda: [])

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.asked.append(request.tool_use_id)
        decision: object = {"allow": True, "reason": "allowed"}
        return cast("PolicyDecision", decision)


def adapter_for(
    documents: list[JsonObject], ctx: ProtocolContext, output_format: bool = True
) -> FakeHarness:
    scope = CallScope(
        harness="claude",
        workspace=workspace("/kora-fake-workspace"),
        node_path="graph.node",
        layers=layers(deny=("Bash(rm *)",)),
        protected_paths=(),
        output_format=output_format,
    )
    return FakeHarness(scripted(documents), scope, ctx)


def shell(tool_use_id: str, command: str) -> JsonObject:
    return tool_call("Bash", tool_use_id, arguments={"command": command})


def denials(sink: ScriptedSink) -> list[PermissionDeniedData]:
    return [
        event.data
        for event in sink.events
        if isinstance(event.data, PermissionDeniedData)
    ]


def results_for(sink: ScriptedSink, tool_use_id: str) -> int:
    return sum(
        1
        for event in sink.events
        if event.kind == "tool_result"
        and getattr(event.data, "tool_use_id", None) == tool_use_id
    )


def test_fake_tool_calls_go_through_policy_allow_and_deny(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    sink = ScriptedSink()
    documents = [turn("m1"), shell("call-1", "ls"), shell("call-2", DENIED_COMMAND)]
    adapter = adapter_for([*documents, result()], ctx)

    outcome = asyncio.run(played(adapter, sink, client))

    assert client.asked == ["call-1", "call-2"]
    assert results_for(sink, "call-1") == 1
    assert results_for(sink, "call-2") == 0
    [denial] = denials(sink)
    assert denial.source == "policy"
    assert denial.code == "denied"
    assert denial.pattern == "node.deny.0"
    assert outcome.denials.total == 1
    assert outcome.error_class is None


def test_fake_tool_calls_go_through_policy_after_max_events(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    sink = ScriptedSink(max_events=1)
    adapter = adapter_for([turn("m1"), shell("call-1", "ls"), result()], ctx)

    outcome = asyncio.run(played(adapter, sink, client))

    assert client.asked == []
    assert "tool_call" not in sink.kinds()
    assert outcome.error_class is ErrorClass.EVENTS_EXHAUSTED


def output_call(agent_id: str | None) -> JsonObject:
    return tool_call("StructuredOutput", "call-out", outcome=None, agent_id=agent_id)


def test_fake_tool_calls_go_through_policy_output_channel(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    sink = ScriptedSink()
    adapter = adapter_for([turn("m1"), output_call(None), result()], ctx)

    asyncio.run(played(adapter, sink, client))

    assert client.asked == []
    channel = [event for event in sink.events if event.kind == "tool_call"]
    assert [getattr(event.data, "output_channel", None) for event in channel] == [True]


def test_fake_tool_calls_go_through_policy_output_channel_exceptions(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    nested = AllowDenyClient(ctx)
    unformatted = AllowDenyClient(ctx)
    nested_adapter = adapter_for([turn("m1"), output_call("agent-1"), result()], ctx)
    unformatted_adapter = adapter_for(
        [turn("m1"), output_call(None), result()], ctx, output_format=False
    )

    asyncio.run(played(nested_adapter, ScriptedSink(), nested))
    asyncio.run(played(unformatted_adapter, ScriptedSink(), unformatted))

    assert nested.asked == ["call-out"]
    assert unformatted.asked == ["call-out"]


def test_fake_tool_calls_go_through_policy_malformed_decision(
    kora_capture: Capture,
) -> None:
    client = MalformedClient()
    sink = ScriptedSink()
    adapter = adapter_for(
        [turn("m1"), shell("call-1", "ls"), result()], context_for(kora_capture)
    )

    outcome = asyncio.run(played(adapter, sink, client))

    assert client.asked == ["call-1"]
    assert outcome.stop_reason == "policy_error"
    assert outcome.error_class is ErrorClass.CONFIG_ERROR
    assert results_for(sink, "call-1") == 0


def nested(depth: int) -> JsonValue:
    value: JsonValue = "leaf"
    for _ in range(depth):
        value = {"next": value}
    return value


def test_fake_tool_calls_go_through_policy_refused_request(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    sink = ScriptedSink()
    deep = tool_call("Bash", "call-1", arguments={"command": nested(40)})
    adapter = adapter_for([turn("m1"), deep, result()], ctx)

    outcome = asyncio.run(played(adapter, sink, client))

    assert client.asked == []
    [denial] = denials(sink)
    assert denial.code == "request_invalid"
    assert outcome.stop_reason == "policy_error"
    assert outcome.error_class is ErrorClass.CONFIG_ERROR


def test_fake_tool_calls_go_through_policy_near_names(kora_capture: Capture) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    calls = [
        tool_call("mcp__x__StructuredOutput", "call-1", outcome=None),
        tool_call("structuredoutput", "call-2", outcome=None),
    ]
    adapter = adapter_for([turn("m1"), *calls, result()], ctx)

    asyncio.run(played(adapter, ScriptedSink(), client))

    assert client.asked == ["call-1", "call-2"]


def test_fake_tool_calls_go_through_policy_oversized_request(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    sink = ScriptedSink()
    large = tool_call("Bash", "call-1", arguments={"command": "x" * 1_100_000})
    adapter = adapter_for([turn("m1"), large, result()], ctx)

    outcome = asyncio.run(played(adapter, sink, client))

    assert client.asked == []
    [denial] = denials(sink)
    assert denial.code == "request_invalid"
    assert outcome.stop_reason == "policy_error"


def test_fake_tool_calls_go_through_policy_refused_after_max_events(
    kora_capture: Capture,
) -> None:
    ctx = context_for(kora_capture)
    client = AllowDenyClient(ctx)
    sink = ScriptedSink(max_events=1)
    deep = tool_call("Bash", "call-1", arguments={"command": nested(40)})
    adapter = adapter_for([turn("m1"), deep, result()], ctx)

    outcome = asyncio.run(played(adapter, sink, client))

    assert client.asked == []
    assert sink.kinds() == ["turn", "result"]
    assert outcome.error_class is ErrorClass.EVENTS_EXHAUSTED
