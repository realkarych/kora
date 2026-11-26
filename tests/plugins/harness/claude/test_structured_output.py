import json
from dataclasses import dataclass
from json.encoder import encode_basestring
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.harness import ConformanceSink, Trace
from kora.ir import ErrorClass
from kora.observability.redaction import MASK, SecretRegistry
from kora.plugins.harness.claude.adapter import ClaudeSummary
from kora.plugins.harness.claude.events import ClaudeEvents
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    MAX_TEXT_BYTES,
    ErrorEvent,
    RunnerOutcome,
    ToolCallEvent,
)
from kora.protocols.time import SystemClock
from tests.plugins.harness.claude import test_policy_bridge
from tests.plugins.harness.claude.conformance_launch import RECORDINGS
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    WHEN,
    context_for,
    init_fields,
    projector,
)
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_session_lifecycle import (
    StoppingSink,
    run_lifecycle,
)

PENDING_LEGS: dict[str, str] = {
    "schema-invalid-payload": "#199",
    "schema-invalid-text": "#199",
    "error-summary": "#199",
    "diagnostic-error-event": "#199",
    "artifact-reference-contract": "#199",
    "scripted-model": "#1388 trust-check and no-network sub-issue",
}
POLICY_LEGS: dict[str, str] = {
    "namespace-deny": "test_adapter_exception_order",
    "mcp-structured-output": "test_adapter_exception_order",
    "subagent-structured-output": "test_adapter_exception_order",
    "without-output-format": "test_adapter_exception_order",
}


@dataclass(frozen=True)
class OutputCase:
    leg: str
    structured_output: object = None
    text: str | None = None
    subtype: str = "success"
    is_error: bool = False
    error_class: ErrorClass | None = None
    diagnostic: str | None = None
    source: str | None = None
    value: object = None
    errors: tuple[str, ...] = ()


CASES: tuple[OutputCase, ...] = (
    OutputCase(
        "tool-payload",
        {"ok": True},
        '{"fallback":true}',
        source="structured_output",
        value={"ok": True},
    ),
    OutputCase(
        "payload-under-failure",
        {"ok": True},
        is_error=True,
        error_class=ErrorClass.HARNESS_ERROR,
    ),
    OutputCase(
        "unrepresentable-payload",
        json.loads("[" * 17 + "0" + "]" * 17),
        '{"fallback":true}',
        diagnostic="structured_output_invalid",
    ),
    OutputCase("valid-text", text='{"a":1}', source="result_text", value={"a": 1}),
    *(
        OutputCase(leg, text=text, diagnostic="output_invalid_json")
        for leg, text in (
            ("fenced-json", "```json\n{}\n```"),
            ("prose", "answer {}"),
            ("nan", "NaN"),
            ("overflow", "1e400"),
            ("duplicate-member", '{"k":1,"k":2}'),
            ("empty-text", ""),
            ("whitespace-text", " "),
        )
    ),
    OutputCase("no-text", diagnostic="output_missing"),
    OutputCase(
        "structured-retries",
        subtype="error_max_structured_output_retries",
        is_error=True,
        error_class=ErrorClass.ARTIFACT_INVALID,
    ),
    OutputCase(
        "max-turns",
        subtype="error_max_turns",
        is_error=True,
        error_class=ErrorClass.EVENTS_EXHAUSTED,
    ),
    OutputCase(
        "cli-errors",
        text='{"a":1}',
        source="result_text",
        value={"a": 1},
        errors=("first", "second"),
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.leg)
async def test_claude_structured_output_fallback_validates_schema(
    claude_capture: Capture, tmp_path: Path, case: OutputCase
) -> None:
    lines = (
        (RECORDINGS / "conformance-structured_output-2.1.301.ndjson")
        .read_text()
        .splitlines()
    )
    terminal: dict[str, object] = json.loads(lines[-1])
    terminal.update(
        subtype=case.subtype,
        is_error=case.is_error,
        terminal_reason=None,
        structured_output=case.structured_output,
        result=case.text,
        errors=list(case.errors),
    )
    lines[-1] = json.dumps(terminal)
    recording = tmp_path / f"{case.leg}.ndjson"
    recording.write_text("\n".join(lines) + "\n")
    sink = StoppingSink()

    result, _, _adapter = await run_lifecycle(
        claude_capture,
        "structured_output",
        recording=recording,
        output_format=True,
        sink=sink,
    )

    assert result.error_class is case.error_class
    assert result.output_diagnostic == case.diagnostic
    if case.source is None:
        assert result.output is None
    else:
        assert result.output is not None
        assert result.output.source == case.source
        assert result.output.value == case.value
    output_calls = [event for event in sink.events if isinstance(event, ToolCallEvent)]
    assert len(output_calls) == 1
    assert output_calls[0].data.tool == "StructuredOutput"
    assert output_calls[0].data.output_channel is True
    errors = [event for event in sink.events if isinstance(event, ErrorEvent)]
    assert [event.data.text for event in errors] == list(case.errors)
    assert all(event.data.code == "harness_result_error" for event in errors)
    result_indices = [event.i for event in sink.events if event.kind == "result"]
    assert len(result_indices) == 1
    assert all(event.i > result_indices[0] for event in errors)


def test_pending_legs_are_owned() -> None:
    case_ids = {case.leg for case in CASES}

    pending = set(PENDING_LEGS)

    assert pending.isdisjoint(case_ids)
    assert all(owner.startswith("#") for owner in PENDING_LEGS.values())


def test_policy_legs_have_existing_coverage() -> None:
    names = set(POLICY_LEGS.values())

    functions = [getattr(test_policy_bridge, name, None) for name in names]

    assert all(callable(function) for function in functions)


@pytest.mark.parametrize("output_format", [True, False])
@pytest.mark.parametrize(
    ("text", "diagnostic"),
    [
        (None, "output_missing"),
        (1, "output_missing"),
        ("answer {}", "output_invalid_json"),
    ],
    ids=["absent", "not-a-string", "invalid-text"],
)
def test_finalise_reports_missing_output(
    claude_capture: Capture, output_format: bool, text: object, diagnostic: str
) -> None:
    adapter = make_adapter(
        Path("/work"), context_for(claude_capture, SystemClock()), output_format
    )
    _, events = projector()
    events.observe_init(init_fields())
    events.observe_result("success", False, None, {}, result=text)

    result = adapter.finalise(
        ClaudeSummary("claude", events),
        RunnerOutcome("completed", "teardown_after_result", 0, None),
    )

    assert result.error_class is None
    assert result.output is None
    assert result.output_diagnostic == (diagnostic if output_format else None)


def test_result_errors_become_error_events() -> None:
    canary = "structured-output-secret"
    registry = SecretRegistry()
    registration = registry.register(canary)
    sink = ConformanceSink(0, Trace())
    events = ClaudeEvents(
        sink,
        registry,
        binding="claude-test",
        max_retries=2,
        output_format=True,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())

    try:
        events.observe_result(
            "success",
            False,
            None,
            {},
            errors=[f"first {canary}", "x" * (MAX_TEXT_BYTES * 2)],
        )
        events.finish()
    finally:
        registration.release()

    errors = [event for event in sink.events if isinstance(event, ErrorEvent)]
    assert [event.kind for event in sink.events] == [
        "session_started",
        "result",
        "error",
        "error",
    ]
    assert len(errors) == 2
    assert all(event.data.code == "harness_result_error" for event in errors)
    assert errors[0].data.text == f"first {MASK}"
    assert errors[0].data.truncated is False
    assert errors[1].data.truncated is True
    assert errors[1].data.text is not None
    assert len(encode_basestring(errors[1].data.text).encode("utf-8")) <= MAX_TEXT_BYTES
    assert canary not in json.dumps(
        [event.model_dump(mode="json") for event in sink.events]
    )


@pytest.mark.parametrize(
    "errors", [[1], ["valid", 1], "invalid", {"error": "invalid"}, ("invalid",)]
)
def test_result_errors_refuse_malformed_values(errors: object) -> None:
    sink, events = projector()
    events.observe_init(init_fields())

    with pytest.raises(KoraProtocolError) as caught:
        events.observe_result("success", False, None, {}, errors=errors)

    assert caught.value.code == "harness_error"
    assert caught.value.details == {}
    assert [event.kind for event in sink.events] == ["session_started"]
    assert events.observation().result_seen is False
