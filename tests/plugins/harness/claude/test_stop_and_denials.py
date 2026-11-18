import json
import os
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import utc_now
from kora.conformance.harness import ConformanceSink
from kora.ir import ErrorClass
from kora.ir.json import parse_json_object
from kora.observability import LoggingSettings
from kora.plugins.harness.claude.denials import harness_denials
from kora.plugins.harness.claude.events import ToolPiece
from kora.plugins.harness.claude.session import COMPONENT
from kora.plugins.harness.claude.stop import ClaudeObservation
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import MAX_SAMPLE_ITEMS, PermissionDeniedEvent, RunnerCause
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from tests.plugins.harness.claude.test_replay_events import (
    assert_session_records,
    init_fields,
    projector,
    rendered_events,
)
from tests.plugins.harness.claude.test_session_lifecycle import (
    StoppingSink,
    run_lifecycle,
)

RECORDINGS = Path(__file__).resolve().parent / "recordings"
CANARY = "hidden-tool-input-stop-denials"
PENDING_LEGS: dict[str, str] = {}


@dataclass(frozen=True)
class StopCase:
    leg: str
    recording: str | None
    cause: RunnerCause
    expected: ErrorClass | None


STOP_CASES = (
    StopCase("success", "success-false", "completed", None),
    StopCase("success-error", "success-true", "completed", ErrorClass.HARNESS_ERROR),
    *(
        StopCase(f"{subtype}-{error}", f"{subtype}-{error}", "completed", expected)
        for subtype, expected in (
            ("error_max_budget_usd", ErrorClass.BUDGET_EXHAUSTED),
            ("error_max_turns", ErrorClass.EVENTS_EXHAUSTED),
            ("error_max_structured_output_retries", ErrorClass.ARTIFACT_INVALID),
            ("error_during_execution", ErrorClass.HARNESS_ERROR),
            ("unknown", ErrorClass.HARNESS_ERROR),
        )
        for error in ("false", "true")
    ),
    StopCase(
        "terminal-max-turns",
        "terminal-max-turns",
        "completed",
        ErrorClass.EVENTS_EXHAUSTED,
    ),
    *(
        StopCase(error, error, "completed", ErrorClass.CONFIG_ERROR)
        for error in (
            "authentication_failed",
            "billing_error",
            "account_on_hold",
            "credits_required",
        )
    ),
    StopCase("overloaded", "overloaded", "completed", ErrorClass.HARNESS_ERROR),
    *(
        StopCase(signal, signal, "completed", ErrorClass.PROVIDER_LIMITED)
        for signal in ("rate_limit", "rate-rejected", "api-429")
    ),
    StopCase("retry-success", "retry-success", "completed", None),
    StopCase(
        "provider-interrupt",
        "interrupt-aborted_tools",
        "provider_limit",
        ErrorClass.PROVIDER_LIMITED,
    ),
    StopCase(
        "cancel-interrupt",
        "interrupt-aborted_streaming",
        "cancel",
        ErrorClass.CANCELLED,
    ),
    StopCase(
        "policy-bypass", "undecided-success", "completed", ErrorClass.HARNESS_ERROR
    ),
    StopCase(
        "limit-then-bypass", "undecided-success", "budget", ErrorClass.HARNESS_ERROR
    ),
    StopCase(
        "limit-then-provider",
        "limit-then-provider",
        "budget",
        ErrorClass.BUDGET_EXHAUSTED,
    ),
    StopCase(
        "provider-cause-bypass",
        "undecided-success",
        "provider_limit",
        ErrorClass.HARNESS_ERROR,
    ),
    StopCase(
        "provider-and-bypass", "rate-bypass", "completed", ErrorClass.HARNESS_ERROR
    ),
    StopCase("launch-rejected", None, "launch_rejected", ErrorClass.CONFIG_ERROR),
    StopCase("kill-no-result", "no-result", "deadline", ErrorClass.TIMEOUT),
    StopCase(
        "cli-exit-no-result",
        "no-result",
        "cli_exit_without_result",
        ErrorClass.HARNESS_ERROR,
    ),
    StopCase("launcher-death", "no-result", "launcher_died", ErrorClass.INFRASTRUCTURE),
    StopCase("failed-launch", None, "launch_failed", ErrorClass.INFRASTRUCTURE),
    StopCase("completed-no-result", "no-result", "completed", ErrorClass.HARNESS_ERROR),
)


@dataclass(frozen=True)
class DenialCase:
    leg: str
    total: int = 0
    sampled: int = 0
    unmatched: int = 0
    complete: bool = True
    bypass: bool = False
    unconfirmed: int = 0


DENIAL_CASES = (
    DenialCase("settings-denial", total=1, sampled=1),
    DenialCase("callback-deny", total=1, sampled=1),
    DenialCase("callback-allow", total=1, sampled=1),
    DenialCase("pre-execution"),
    DenialCase("pre-execution-denial", total=1, sampled=1),
    DenialCase("embedded-marker", bypass=True),
    DenialCase("mcp-marker", bypass=True),
    DenialCase("undecided-error", bypass=True),
    DenialCase("held-error", complete=False, unconfirmed=1),
    DenialCase("undecided-success", bypass=True),
    DenialCase("unmatched-denial", unmatched=1),
    DenialCase("oversized-name", total=1, complete=False),
    DenialCase("unknown-marker", bypass=True),
)
SECTION_13_LEGS = frozenset(
    {
        "success",
        "success-error",
        "terminal-max-turns",
        "authentication_failed",
        "billing_error",
        "account_on_hold",
        "credits_required",
        "overloaded",
        "rate_limit",
        "rate-rejected",
        "api-429",
        "retry-success",
        "provider-interrupt",
        "cancel-interrupt",
        "policy-bypass",
        "limit-then-bypass",
        "provider-cause-bypass",
        "limit-then-provider",
        "provider-and-bypass",
        "launch-rejected",
        "kill-no-result",
        "cli-exit-no-result",
        "launcher-death",
        "failed-launch",
        "completed-no-result",
        "settings-denial",
        "callback-deny",
        "callback-allow",
        "pre-execution",
        "pre-execution-denial",
        "embedded-marker",
        "mcp-marker",
        "undecided-error",
        "held-error",
        "undecided-success",
        "unmatched-denial",
        "oversized-name",
        "unknown-marker",
        "scripted-execution-error",
        "bypass-interrupt",
        "success-racing-real-interrupt",
        "error_max_budget_usd-false",
        "error_max_budget_usd-true",
        "error_max_turns-false",
        "error_max_turns-true",
        "error_max_structured_output_retries-false",
        "error_max_structured_output_retries-true",
        "error_during_execution-false",
        "error_during_execution-true",
        "unknown-false",
        "unknown-true",
    }
)


@pytest.fixture
def stop_capture() -> Generator[Capture]:
    with captured(COMPONENT, LoggingSettings(), "claude-stop", utc_now) as capture:
        yield capture


def recording_path(name: str) -> Path:
    return RECORDINGS / f"constructed-{name}-2.1.301.ndjson"


def unlaunched_observation() -> tuple[ConformanceSink, ClaudeObservation]:
    sink, events = projector()
    return sink, events.observation()


class RecordingPolicy:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.calls: list[tuple[str, str]] = []

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        self.calls.append((request.tool, request.tool_use_id))
        return PolicyDecision(self.allow, "allowed" if self.allow else "denied")


class FailingPolicy:
    def __init__(self, failure: str) -> None:
        self.failure = failure

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        if self.failure == "exception":
            raise ValueError(CANARY)
        return cast(PolicyDecision, object())


def assert_decision(decisions: tuple[str, ...], expected: str) -> None:
    assert len(decisions) == 1
    decision = parse_json_object(json.loads(decisions[0]))
    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": expected,
            "permissionDecisionReason": "kora",
        }
    }


async def replay_recording(
    name: str | None, capture: Capture
) -> tuple[ConformanceSink, ClaudeObservation]:
    if name is None:
        return unlaunched_observation()
    sink = StoppingSink()
    policy = RecordingPolicy(name != "callback-deny")

    _result, outcome, adapter = await run_lifecycle(
        capture,
        "replay",
        recording=recording_path(name),
        sink=sink,
        policy=policy,
    )

    expected_cause = (
        "cli_exit_without_result"
        if name in {"held-error", "no-result"}
        else "policy_bypass"
        if name
        in {
            "embedded-marker",
            "mcp-marker",
            "undecided-error",
            "undecided-success",
            "unknown-marker",
            "rate-bypass",
        }
        else "completed"
    )
    assert outcome.cause == expected_cause
    assert_session_records(capture.finish(), (CANARY,), expected_cause)
    assert policy.calls == (
        [("Bash", "call-1")] if name.startswith("callback-") else []
    )
    if name.startswith("callback-"):
        assert_decision(
            adapter.decisions, "deny" if name == "callback-deny" else "allow"
        )
    assert CANARY not in rendered_events(sink)
    assert adapter.observation is not None
    return sink, adapter.observation


@pytest.mark.asyncio
@pytest.mark.parametrize("case", STOP_CASES, ids=[case.leg for case in STOP_CASES])
async def test_claude_stop_reason_mapping(
    case: StopCase, stop_capture: Capture
) -> None:
    expected = case.expected

    _sink, observation = await replay_recording(case.recording, stop_capture)

    assert observation.error_class(case.cause) == expected
    if case.leg == "limit-then-provider":
        assert observation.provider_limit_seen
    assert observation.api_error_status == (
        429 if case.recording == "api-429" else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DENIAL_CASES, ids=[case.leg for case in DENIAL_CASES])
async def test_claude_harness_denials_are_recorded(
    case: DenialCase, stop_capture: Capture
) -> None:
    expected = case

    sink, observation = await replay_recording(case.leg, stop_capture)

    assert_denial(sink, observation, expected)


def assert_denial(
    sink: ConformanceSink, observation: ClaudeObservation, case: DenialCase
) -> None:
    denials = [
        event for event in sink.events if isinstance(event, PermissionDeniedEvent)
    ]
    assert len(denials) == case.sampled
    assert observation.denials.total == case.total
    assert len(observation.denials.items) == case.sampled
    assert observation.unmatched_denials == case.unmatched
    assert observation.denials_complete is case.complete
    assert observation.bypass.confirmed is case.bypass
    assert observation.bypass.unconfirmed.total == case.unconfirmed
    assert CANARY not in rendered_events(sink)
    for event in denials:
        assert event.data.source == (
            "policy" if case.leg == "callback-deny" else "harness"
        )
        assert event.data.pattern is None
        assert event.data.code == (
            "denied" if case.leg == "callback-deny" else "harness_rule"
        )
        assert event.data.tool_use_id == "call-1"
        assert sink.events.index(event) < len(sink.events) - 1
    if case.unconfirmed:
        assert observation.error_class("deadline") == ErrorClass.TIMEOUT


def test_section_13_legs_have_cases_or_pending_owners() -> None:
    case_ids = {case.leg for case in STOP_CASES} | {case.leg for case in DENIAL_CASES}

    covered = (
        case_ids
        | PENDING_LEGS.keys()
        | {
            "bypass-interrupt",
            "success-racing-real-interrupt",
            "scripted-execution-error",
        }
    )

    assert SECTION_13_LEGS == covered
    assert not case_ids.intersection(PENDING_LEGS)
    assert all(owner.startswith("#") for owner in PENDING_LEGS.values())


@pytest.mark.parametrize(
    "entries", [{}, [None], [{}], [{"tool_name": 1, "tool_use_id": "call"}]]
)
def test_malformed_denials_fail_without_content(entries: object) -> None:
    observed = {"call": "Bash"}

    with pytest.raises(KoraProtocolError) as caught:
        harness_denials(entries, observed, set())

    assert caught.value.code == "harness_error"
    assert caught.value.details == {}


def test_denial_sample_overflow_preserves_held_classification() -> None:
    _sink, events = projector()
    events.observe_init(init_fields())
    entries: list[dict[str, object]] = []
    for index in range(MAX_SAMPLE_ITEMS + 1):
        identity = f"call-{index}"
        events.assistant(f"message-{index}", None, (ToolPiece("Bash", identity),))
        if index < MAX_SAMPLE_ITEMS:
            events.decided(identity, True)
        events.tool_result(identity, True, "denied")
        entries.append(
            {"tool_name": "Bash", "tool_use_id": identity, "tool_input": CANARY}
        )

    events.observe_result("success", False, None, None, permission_denials=entries)
    events.finish()
    observation = events.observation()

    assert observation.denials.total == MAX_SAMPLE_ITEMS + 1
    assert len(observation.denials.items) == MAX_SAMPLE_ITEMS
    assert observation.denials_complete is False
    assert observation.bypass.confirmed is False


def test_held_error_overflow_is_a_bypass_without_result() -> None:
    _sink, events = projector()
    events.observe_init(init_fields())

    for index in range(MAX_SAMPLE_ITEMS + 1):
        events.tool_result(f"call-{index}", True, "failed")
    observation = events.observation()

    assert observation.bypass.confirmed is True
    assert observation.bypass.unconfirmed.total == MAX_SAMPLE_ITEMS
    assert observation.denials_complete is False
    assert observation.error_class("budget") == ErrorClass.HARNESS_ERROR


@pytest.mark.parametrize(
    "tool", ["", "x" * 257, "has space", "nonascii-λ", "line\nbreak"]
)
def test_unrepresentable_denial_names_are_only_counted(tool: str) -> None:
    entry = {"tool_name": tool, "tool_use_id": "call", "tool_input": CANARY}

    denials = harness_denials([entry], {"call": "Bash"}, set())

    assert denials.sample.total == 1
    assert denials.sample.items == ()
    assert denials.matched == {"call"}
    assert denials.unmatched == 0


def test_conflicting_tool_identity_is_refused() -> None:
    _sink, events = projector()
    events.observe_init(init_fields())
    events.assistant("message", None, (ToolPiece("mcp__server__tool", "call"),))

    with pytest.raises(KoraProtocolError) as caught:
        events.assistant("message", None, (ToolPiece("Bash", "call"),))

    assert caught.value.code == "harness_error"


def test_non_error_bypass_is_visible_before_result() -> None:
    _sink, events = projector()
    events.observe_init(init_fields())
    events.assistant("message", None, (ToolPiece("Bash", "call"),))

    events.tool_result("call", False, "done")

    assert events.bypass_confirmed is True
    assert events.observation().result_seen is False


@pytest.mark.parametrize("response", [None, "wrong-id", "hook-request-1"])
def test_player_requires_matching_hook_response(
    tmp_path: Path, response: str | None
) -> None:
    player = Path(__file__).with_name("conformance_player.py")
    recording = tmp_path / "recording.ndjson"
    recording.write_bytes(recording_path("callback-allow").read_bytes())
    messages: list[dict[str, object]] = [
        {
            "type": "control_request",
            "request_id": "initialize",
            "request": {"subtype": "initialize"},
        },
        {"type": "user"},
    ]
    if response is not None:
        messages.append(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": response,
                    "response": {},
                },
            }
        )
    request = "".join(json.dumps(message) + "\n" for message in messages)

    read_fd, write_fd = os.pipe()

    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(player), "2.1.301", str(write_fd)],
            input=request,
            text=True,
            capture_output=True,
            env={"KORA_CLAUDE_REPLAY_RECORDING": str(recording)},
            pass_fds=(write_fd,),
            timeout=10,
            check=False,
        )
    finally:
        os.close(write_fd)
        os.close(read_fd)

    assert (completed.returncode == 0) is (response != "wrong-id")
    kinds = [
        parse_json_object(json.loads(line))["type"]
        for line in completed.stdout.splitlines()
    ]
    assert "control_request" in kinds
    assert ("result" in kinds) is (response == "hook-request-1")
    assert recording.with_suffix(".decisions").exists() is (
        response == "hook-request-1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "invalid-decision"])
async def test_sdk_hook_failure_denies_without_sending_exception_text(
    stop_capture: Capture, failure: str
) -> None:
    sink = StoppingSink()
    policy = FailingPolicy(failure)

    result, outcome, adapter = await run_lifecycle(
        stop_capture,
        "replay",
        recording=recording_path("callback-allow"),
        sink=sink,
        policy=policy,
    )

    assert_decision(adapter.decisions, "deny")
    assert outcome.cause == "policy_error"
    assert CANARY not in rendered_events(sink)
    assert CANARY not in repr(result)
    records = stop_capture.finish()
    assert_session_records(records, (CANARY,), "policy_error")


@pytest.mark.asyncio
async def test_bypass_interrupt(stop_capture: Capture) -> None:

    result, outcome, _adapter = await run_lifecycle(stop_capture, "bypass")

    assert result.policy_bypass
    assert result.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.cause == "policy_bypass"
    assert outcome.termination == "interrupted"


@pytest.mark.asyncio
async def test_success_racing_real_interrupt(stop_capture: Capture) -> None:

    result, outcome, _adapter = await run_lifecycle(
        stop_capture, "race", cause="deadline"
    )

    assert result.error_class is ErrorClass.TIMEOUT
    assert outcome.cause == "deadline"
    assert outcome.termination == "interrupted"


def test_scripted_execution_error(tmp_path: Path) -> None:
    from tests.plugins.harness.claude.contained import assert_no_outbound
    from tests.plugins.harness.claude.scripted_model import mapping, objects
    from tests.plugins.harness.claude.test_session_isolation import run_scenario

    workspace, account, markers = (
        tmp_path / name for name in ("workspace", "account", "markers")
    )
    for path in (workspace, account, markers):
        path.mkdir()

    report = run_scenario(
        tmp_path, workspace, account, markers, [("Bash", {"command": "exit 3"})]
    )

    assert_no_outbound(report)
    assert report["cause"] == "completed"
    assert report["error_class"] is None
    assert report["policy_bypass"] is False
    assert (mapping(report["denials"]) or {})["total"] == 0
    events = [mapping(event) or {} for event in objects(report["events"]) or []]
    results = [
        mapping(event["data"]) or {}
        for event in events
        if event.get("kind") == "tool_result"
    ]
    assert len(results) == 1
    assert results[0]["is_error"] is True
    text = results[0]["content"]
    assert isinstance(text, str)
    assert text
    assert not text.startswith("<tool_use_error>")
    assert not any(event.get("kind") == "permission_denied" for event in events)
