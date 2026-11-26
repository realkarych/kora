import asyncio
from pathlib import Path
from typing import Literal

import pytest
from claude_agent_sdk.types import PreToolUseHookInput

from kora.conformance.capture import Capture
from kora.conformance.harness import ConformancePolicy, ConformanceSink, Trace
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.claude.events import ClaudeEvents, TextPiece, ToolPiece
from kora.plugins.harness.claude.policy import PolicyBridge
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import PermissionDeniedData, RunnerCause
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from kora.protocols.time import SystemClock
from kora.runner.calls import MAX_DECIDED, PolicyCallFailed
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    WHEN,
    context_for,
    init_fields,
)
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)

PENDING_LEGS = {"codex": "#772 Codex adapter exception order"}


def hook_input(tool: str, agent_id: str | None = None) -> PreToolUseHookInput:
    value: PreToolUseHookInput = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_use_id": "call",
        "tool_input": {"command": "fixture"},
        "session_id": "session",
        "cwd": "/work",
        "transcript_path": "/unused",
    }
    if agent_id is not None:
        value["agent_id"] = agent_id
    return value


class FailingPolicy:
    def __init__(
        self, failure: Literal["error", "timeout", "callback", "cancel"]
    ) -> None:
        self.failure = failure
        self.entered = asyncio.Event()

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.entered.set()
        if self.failure == "error":
            raise KoraProtocolError(
                "fixture_failure", "private-policy-content", False, {}
            )
        if self.failure == "timeout":
            raise PolicyCallFailed("timeout")
        if self.failure == "cancel":
            await asyncio.Event().wait()
        raise ValueError("private-policy-content")


class ConcurrentPolicy:
    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self.arrivals = 0
        self.both_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.arrivals += 1
        if self.arrivals == self.limit:
            self.both_entered.set()
        await self.release.wait()
        return PolicyDecision(True, "allowed")


@pytest.mark.asyncio
async def test_concurrent_policy_rechecks_event_limit(claude_capture: Capture) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    sink = ConformanceSink(2, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    events.assistant("message", None, ())
    events.flush()
    stops: list[RunnerCause] = []
    policy = ConcurrentPolicy()
    bridge = PolicyBridge(adapter.scope, policy, sink, events, stops.append)
    tasks = [
        asyncio.create_task(bridge.hook(hook_input("Bash"), identity, {"signal": None}))
        for identity in ("first", "second")
    ]

    async with asyncio.timeout(5):
        await policy.both_entered.wait()
        policy.release.set()
        replies = await asyncio.gather(*tasks)

    assert (
        sum(
            reply.get("hookSpecificOutput")
            == {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "kora",
            }
            for reply in replies
        )
        == 1
    )
    assert sum(event.kind == "tool_call" for event in sink.events) == 1
    assert events.calls.decided_ids == {"first", "second"}
    assert stops
    assert set(stops) == {"max_events"}
    assert all(task.done() for task in bridge.active)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "output_format", "agent", "allowed", "decisions", "exhausted"),
    [
        pytest.param(
            "StructuredOutput", True, None, True, 0, False, id="output-channel"
        ),
        pytest.param(
            "mcp__x__StructuredOutput", True, None, False, 1, False, id="mcp-name"
        ),
        pytest.param(
            "StructuredOutput", False, None, False, 1, False, id="no-output-format"
        ),
        pytest.param(
            "StructuredOutput", True, "subagent", False, 1, False, id="subagent"
        ),
        pytest.param(
            "StructuredOutput", True, None, False, 0, True, id="output-after-max-events"
        ),
        pytest.param("Bash", True, None, False, 0, True, id="bash-after-max-events"),
    ],
)
async def test_adapter_exception_order(
    claude_capture: Capture,
    tool: str,
    output_format: bool,
    agent: str | None,
    allowed: bool,
    decisions: int,
    exhausted: bool,
) -> None:
    adapter = make_adapter(
        Path("/work"), context_for(claude_capture, SystemClock()), output_format
    )
    sink = ConformanceSink(1 if exhausted else None, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=output_format,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    stops: list[RunnerCause] = []
    denied_tool = "Forbidden" if exhausted else tool
    bridge = PolicyBridge(
        adapter.scope,
        ConformancePolicy(denied_tool, sink.trace),
        sink,
        events,
        stops.append,
    )
    if exhausted:
        events.assistant("message", None, ())
        events.flush()
    else:
        events.assistant("message", agent, (TextPiece("before callback"),))

    reply = await asyncio.create_task(
        bridge.hook(hook_input(tool, agent), "call", {"signal": None})
    )
    if not exhausted:
        events.assistant("message", agent, (ToolPiece(tool, "call"),))
        events.tool_result("call", not allowed, "done")
        events.finish()

    assert reply.get("hookSpecificOutput") == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": "kora",
    }
    assert "call" in events.calls.decided_ids
    if exhausted:
        assert all(
            kind not in {"tool_call", "decide", "permission_denied"}
            for kind, _ in sink.trace.entries
        )
        return
    assert sum(kind == "decide" for kind, _ in sink.trace.entries) == decisions
    calls = [event for event in sink.events if event.kind == "tool_call"]
    assert len(calls) == 1
    assert sum(event.kind == "turn" for event in sink.events) == events.turns == 1
    assert calls[0].data.output_channel is allowed
    assert not events.bypass_confirmed
    assert stops == []
    if decisions:
        assert sink.trace.entries.index(("decide", "call")) < sink.trace.entries.index(
            ("tool_result", "call")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        ("error", "policy_error"),
        ("timeout", "policy_timeout"),
        ("callback", "policy_error"),
    ],
)
async def test_policy_failure_fails_closed(
    claude_capture: Capture,
    failure: Literal["error", "timeout", "callback"],
    cause: RunnerCause,
) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    sink = ConformanceSink(None, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    stops: list[RunnerCause] = []
    bridge = PolicyBridge(
        adapter.scope, FailingPolicy(failure), sink, events, stops.append
    )

    reply = await asyncio.create_task(
        bridge.hook(hook_input("Bash"), "call", {"signal": None})
    )

    assert reply.get("hookSpecificOutput") == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "kora",
    }
    assert stops == [cause]
    calls = [event.data for event in sink.events if event.kind == "tool_call"]
    denials = [event.data for event in sink.events if event.kind == "permission_denied"]
    assert len(calls) == len(denials) == 1
    assert calls[0].tool == denials[0].tool == "Bash"
    assert calls[0].tool_use_id == denials[0].tool_use_id == "call"
    assert not calls[0].output_channel
    assert denials[0].source == "harness"
    assert denials[0].code == cause
    assert "private-policy-content" not in repr(sink.events)
    assert "private-policy-content" not in repr(reply)
    assert "private-policy-content" not in repr(events.observation())
    assert all(task.done() for task in bridge.active)


@pytest.mark.asyncio
async def test_policy_cancellation_propagates(claude_capture: Capture) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    sink = ConformanceSink(None, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    stops: list[RunnerCause] = []
    policy = FailingPolicy("cancel")
    bridge = PolicyBridge(adapter.scope, policy, sink, events, stops.append)
    task = asyncio.create_task(
        bridge.hook(hook_input("Bash"), "call", {"signal": None})
    )

    async with asyncio.timeout(5):
        await policy.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert stops == []
    assert all(task.done() for task in bridge.active)


@pytest.mark.asyncio
async def test_policy_callback_admission_limit(claude_capture: Capture) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    sink = ConformanceSink(None, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    stops: list[RunnerCause] = []
    policy = ConcurrentPolicy(64)
    bridge = PolicyBridge(adapter.scope, policy, sink, events, stops.append)
    tasks = [
        asyncio.create_task(
            bridge.hook(hook_input("Bash"), f"call-{index}", {"signal": None})
        )
        for index in range(64)
    ]

    try:
        async with asyncio.timeout(5):
            await policy.both_entered.wait()
            reply = await bridge.hook(hook_input("Bash"), "overflow", {"signal": None})

        assert reply.get("hookSpecificOutput") == {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "kora",
        }
        assert policy.arrivals == 64
        assert len(bridge.active) == 64
        assert stops == ["policy_error"]
        denials = [
            event.data for event in sink.events if event.kind == "permission_denied"
        ]
        assert len(denials) == 1
        assert denials[0].tool_use_id == "overflow"
        assert denials[0].source == "harness"
        assert denials[0].code == "policy_error"
    finally:
        policy.release.set()
        await asyncio.gather(*tasks)
    assert not bridge.active
    await bridge.hook(hook_input("Bash"), "after-release", {"signal": None})
    assert policy.arrivals == 65


@pytest.mark.asyncio
async def test_callback_failure_at_tracking_capacity_still_denies(
    claude_capture: Capture,
) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    sink = ConformanceSink(None, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    for index in range(MAX_DECIDED):
        events.decided(f"known-{index}", True)
    stops: list[RunnerCause] = []
    bridge = PolicyBridge(
        adapter.scope, FailingPolicy("callback"), sink, events, stops.append
    )

    reply = await bridge.hook(hook_input("Bash"), "overflow", {"signal": None})

    assert reply.get("hookSpecificOutput") == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "kora",
    }
    assert stops == ["policy_error"]
    assert not bridge.active


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", [None, "deadline", "cancel"])
async def test_surface_refusal_denies_waiting_policy_and_later_calls(
    claude_capture: Capture,
    earlier: RunnerCause | None,
) -> None:
    from kora.plugins.harness.claude.run import ClaudeSession
    from tests.plugins.harness.claude.test_init_surface import reviewed_init

    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    sink = ConformanceSink(None, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    policy = ConcurrentPolicy(1)
    session = ClaudeSession(
        adapter.scope, adapter.ctx, events, sink, policy, asyncio.Event()
    )
    session.observe_surface(reviewed_init())
    pending = asyncio.create_task(
        session.bridge.hook(hook_input("Bash"), "pending", {"signal": None})
    )

    async with asyncio.timeout(5):
        await policy.both_entered.wait()
        if earlier is not None:
            session.stop(earlier, external=True)
        session.stop("plugin_mismatch", external=True)
        policy.release.set()
        reply = await pending
        later = await session.bridge.hook(hook_input("Bash"), "later", {"signal": None})

    expected = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "kora",
        }
    }
    assert reply == expected
    assert later == expected
    assert policy.arrivals == 1
    assert events.calls.decided_ids == {"pending", "later"}
    assert session.cause == (earlier or "plugin_mismatch")


@pytest.mark.asyncio
async def test_calls_before_a_checked_init_fail_the_session_without_policy(
    claude_capture: Capture,
) -> None:
    from kora.plugins.harness.claude.run import ClaudeSession

    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    sink = ConformanceSink(None, Trace())
    events = ClaudeEvents(
        sink,
        SecretRegistry(),
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    policy = ConcurrentPolicy(1)
    session = ClaudeSession(
        adapter.scope, adapter.ctx, events, sink, policy, asyncio.Event()
    )

    async with asyncio.timeout(5):
        reply = await session.bridge.hook(hook_input("Bash"), "early", {"signal": None})

    assert reply == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "kora",
        }
    }
    assert policy.arrivals == 0
    assert session.cause == "policy_error"
    assert [event.kind for event in sink.events] == ["tool_call", "permission_denied"]
    denial = sink.events[1].data
    assert isinstance(denial, PermissionDeniedData)
    assert (denial.tool_use_id, denial.source, denial.code) == (
        "early",
        "harness",
        "policy_error",
    )
