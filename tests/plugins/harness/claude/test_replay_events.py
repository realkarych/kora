import hashlib
import json
from collections.abc import Generator
from datetime import UTC, datetime
from json.encoder import encode_basestring
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import utc_now
from kora.conformance.harness import ConformanceSink, Trace
from kora.ir.agent_session_validation import TRUNCATION_MARKER
from kora.observability import LoggingSettings
from kora.observability.redaction import MASK, SecretRegistry
from kora.plugins.harness.claude.events import ClaudeEvents, TextPiece, plugins_digest
from kora.plugins.harness.claude.session import COMPONENT
from kora.plugins.harness.claude.trust import REVIEWED_PLUGINS
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    MAX_TEXT_BYTES,
    ResultEvent,
    SessionStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnEvent,
    UsageEvent,
)
from kora.protocols.time import Clock, SystemClock

RECORDING: Path = (
    Path(__file__).resolve().parent
    / "recordings"
    / "constructed-tool-turn-2.1.301.ndjson"
)
USAGE_RECORDING: Path = (
    Path(__file__).resolve().parent / "recordings" / "constructed-usage-2.1.301.ndjson"
)
RESULT_RECORDING: Path = (
    Path(__file__).resolve().parent / "recordings" / "constructed-result-2.1.301.ndjson"
)
WHEN = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
CANARY = "fixture-replay-secret"
MARKER = TRUNCATION_MARKER.decode("utf-8")
HIDDEN = ("hidden-delta", "hidden-tool-input")


@pytest.fixture
def claude_capture() -> Generator[Capture]:
    with captured(COMPONENT, LoggingSettings(), "claude-replay", utc_now) as capture:
        yield capture


def context_for(capture: Capture, clock: Clock) -> ProtocolContext:
    return ProtocolContext(
        namespace_id="claude-replay",
        run_id="claude-run",
        attempt_id="claude-attempt",
        logger=capture.diagnostics.logger(COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest=None,
        deadline=None,
        clock=clock,
        activity=NoActivity(),
    )


class FrozenClock(SystemClock):
    def now(self) -> datetime:
        return WHEN


def projector() -> tuple[ConformanceSink, ClaudeEvents]:
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
    return sink, events


def init_fields(source: str = "ANTHROPIC_API_KEY") -> dict[str, object]:
    return {
        "session_id": "sess-replay-1",
        "claude_code_version": "2.1.301",
        "apiKeySource": source,
        "plugins": [],
        "mcp_servers": [],
    }


def digest_of(names: list[str]) -> str:
    encoded = json.dumps(
        names,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def rendered_events(sink: ConformanceSink) -> str:
    payloads: list[object] = []
    for event in sink.events:
        payloads.append(event.model_dump(mode="json"))
    return json.dumps(payloads, sort_keys=True)


def refuse_content(rendered: str, secrets: tuple[str, ...], message: str) -> None:
    for secret in secrets:
        if secret in rendered:
            raise AssertionError(message)


def assert_session_records(
    records: list[dict[str, object]], secrets: tuple[str, ...], cause: str = "completed"
) -> None:
    finished = [
        record
        for record in records
        if record.get("event") == "harness.claude.session_finished"
    ]
    assert len(finished) == 1
    assert finished[0].get("outcome") == cause
    rendered = "\n".join(json.dumps(record, sort_keys=True) for record in records)
    refuse_content(rendered, secrets, "session content reached a diagnostic record")


@pytest.mark.asyncio
async def test_claude_replay_projects_a_tool_turn(claude_capture: Capture) -> None:
    from tests.plugins.harness.claude.test_session_lifecycle import (
        StoppingSink,
        run_lifecycle,
    )

    sink = StoppingSink()

    _result, _outcome, _adapter = await run_lifecycle(
        claude_capture,
        "replay",
        recording=RECORDING,
        sink=sink,
        clock=SystemClock(),
    )

    assert [event.kind for event in sink.events] == [
        "session_started",
        "turn",
        "tool_call",
        "tool_result",
        "result",
    ]
    started = sink.events[0]
    turn = sink.events[1]
    call = sink.events[2]
    result = sink.events[3]
    assert isinstance(started, SessionStartedEvent)
    assert isinstance(turn, TurnEvent)
    assert isinstance(call, ToolCallEvent)
    assert isinstance(result, ToolResultEvent)
    assert started.data.session_id == "sess-replay-1"
    assert started.data.harness_version == "2.1.301"
    assert started.data.billing_class == "api_key"
    assert started.data.mcp_sources == ()
    assert started.data.plugins_digest == digest_of(sorted(REVIEWED_PLUGINS))
    assert started.data.plugins_digest == plugins_digest(
        tuple(sorted(REVIEWED_PLUGINS))
    )
    assert turn.data.message_id == "msg-turn-1"
    assert turn.data.text == "hello"
    assert turn.data.truncated is False
    assert call.data.tool == "Write"
    assert call.data.tool_use_id == "toolu-write-1"
    assert call.data.output_channel is False
    assert result.data.tool_use_id == "toolu-write-1"
    assert result.data.content == "wrote"
    assert result.data.is_error is False
    ended = sink.events[4]
    assert isinstance(ended, ResultEvent)
    assert ended.data.subtype == "success"
    assert ended.data.is_error is False
    assert ended.data.terminal_reason is None
    refuse_content(
        rendered_events(sink),
        HIDDEN,
        "session content reached an event",
    )
    assert_session_records(claude_capture.finish(), HIDDEN)


@pytest.mark.asyncio
async def test_claude_replay_masks_turn_text(
    claude_capture: Capture, tmp_path: Path
) -> None:
    from tests.plugins.harness.claude.test_session_lifecycle import (
        StoppingSink,
        run_lifecycle,
    )

    recording = tmp_path / "recording.ndjson"
    init = RECORDING.read_text(encoding="utf-8").splitlines()[0]
    message = {
        "type": "assistant",
        "uuid": "asst-secret",
        "session_id": "sess-replay-1",
        "message": {
            "id": "msg-secret",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": CANARY}],
        },
    }
    recording.write_text(
        init + "\n" + json.dumps(message, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    registry = SecretRegistry()
    registration = registry.register(CANARY)
    sink = StoppingSink()

    try:
        _result, _outcome, _adapter = await run_lifecycle(
            claude_capture,
            "replay",
            recording=recording,
            sink=sink,
            clock=SystemClock(),
            registry=registry,
        )
    finally:
        registration.release()

    turn = sink.events[1]
    assert isinstance(turn, TurnEvent)
    text = turn.data.text
    if text != MASK or text is None or CANARY in text:
        raise AssertionError("session content reached an event")
    refuse_content(
        rendered_events(sink),
        (CANARY,),
        "session content reached an event",
    )
    assert_session_records(
        claude_capture.finish(), (CANARY,), "cli_exit_without_result"
    )


def test_claude_event_projection_counts_one_turn_per_message_id() -> None:
    sink, events = projector()
    events.observe_init(init_fields())
    events.assistant("msg-1", None, (TextPiece("one"),))
    events.assistant("msg-2", None, (TextPiece("two"),))

    events.finish()

    turns = [event for event in sink.events if isinstance(event, TurnEvent)]
    assert [event.kind for event in sink.events] == [
        "session_started",
        "turn",
        "turn",
    ]
    assert [turn.data.message_id for turn in turns] == ["msg-1", "msg-2"]
    assert [turn.data.text for turn in turns] == ["one", "two"]


def test_claude_event_projection_truncates_turn_text() -> None:
    sink, events = projector()
    events.observe_init(init_fields())
    events.assistant("msg-1", None, (TextPiece("a" * MAX_TEXT_BYTES),))

    events.finish()

    turn = sink.events[1]
    assert isinstance(turn, TurnEvent)
    assert turn.data.text is not None
    assert turn.data.truncated is True
    assert turn.data.text.endswith(MARKER)
    assert len(encode_basestring(turn.data.text).encode("utf-8")) == MAX_TEXT_BYTES


@pytest.mark.asyncio
async def test_claude_replay_projects_partial_usage(claude_capture: Capture) -> None:
    from tests.plugins.harness.claude.test_session_lifecycle import (
        StoppingSink,
        run_lifecycle,
    )

    sink = StoppingSink()

    _result, _outcome, _adapter = await run_lifecycle(
        claude_capture,
        "replay",
        recording=USAGE_RECORDING,
        sink=sink,
        clock=FrozenClock(),
    )

    assert [event.kind for event in sink.events] == [
        "session_started",
        "turn",
        "usage",
        "result",
    ]
    turn = sink.events[1]
    usage = sink.events[2]
    assert isinstance(turn, TurnEvent)
    assert isinstance(usage, UsageEvent)
    assert turn.data.text == "hello"
    assert usage.data.message_id == "msg-usage-1"
    assert usage.data.model == "claude-sonnet-4-5"
    assert usage.data.input_tokens == 25
    assert usage.data.cached_input_tokens == 4
    assert usage.data.cache_creation_tokens == 0
    assert usage.data.output_tokens == 12
    assert usage.data.reasoning_tokens == 0
    assert usage.data.known_token_fields == frozenset(
        {
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_tokens",
            "output_tokens",
        }
    )
    assert usage.data.basis == "price_table"
    assert usage.data.billing_class == "api_key"
    assert usage.data.reported_usd is None
    assert usage.data.observation_sequence == 0
    assert usage.data.observed_at == WHEN
    assert usage.data.coverage.completeness == "partial"
    assert usage.data.coverage.attribution_gap is False
    ended = sink.events[3]
    assert isinstance(ended, ResultEvent)
    assert ended.data.subtype == "success"
    assert ended.data.is_error is False
    assert ended.data.terminal_reason is None
    refuse_content(
        rendered_events(sink),
        ("hidden-delta",),
        "session content reached an event",
    )
    assert_session_records(claude_capture.finish(), ("hidden-delta",))


def test_claude_event_projection_keeps_the_last_usage_delta() -> None:
    sink, events = projector()
    events.observe_init(init_fields())
    events.usage_start(
        "msg-1",
        "claude-sonnet-4-5",
        None,
        {
            "input_tokens": 25,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 0,
            "output_tokens": 99,
        },
    )
    events.usage_delta({"output_tokens": 3})
    events.usage_delta({"output_tokens": 12})
    events.usage_start(
        "msg-2",
        "claude-sonnet-4-5",
        None,
        {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )
    events.usage_delta({"output_tokens": 2})

    events.finish()

    usage = [event for event in sink.events if isinstance(event, UsageEvent)]
    assert [event.kind for event in sink.events] == [
        "session_started",
        "usage",
        "usage",
    ]
    assert usage[0].data.message_id == "msg-1"
    assert usage[0].data.input_tokens == 25
    assert usage[0].data.cached_input_tokens == 4
    assert usage[0].data.cache_creation_tokens == 0
    assert usage[0].data.output_tokens == 12
    assert usage[0].data.reasoning_tokens == 0
    assert "reasoning_tokens" not in usage[0].data.known_token_fields
    assert usage[0].data.observation_sequence == 0
    assert usage[0].data.coverage.completeness == "partial"
    assert usage[1].data.message_id == "msg-2"
    assert usage[1].data.input_tokens == 1
    assert usage[1].data.output_tokens == 2
    assert usage[1].data.observation_sequence == 1


@pytest.mark.asyncio
async def test_claude_replay_keeps_per_call_usage_above_a_lower_final_entry(
    claude_capture: Capture,
) -> None:
    from tests.plugins.harness.claude.test_session_lifecycle import (
        StoppingSink,
        run_lifecycle,
    )

    sink = StoppingSink()

    _result, _outcome, adapter = await run_lifecycle(
        claude_capture,
        "replay",
        recording=RESULT_RECORDING,
        sink=sink,
        clock=FrozenClock(),
    )

    assert [event.kind for event in sink.events] == [
        "session_started",
        "turn",
        "usage",
        "result",
    ]
    usage = sink.events[2]
    ended = sink.events[3]
    assert isinstance(usage, UsageEvent)
    assert isinstance(ended, ResultEvent)
    assert usage.data.message_id == "msg-usage-1"
    assert usage.data.model == "claude-sonnet-4-5"
    assert usage.data.input_tokens == 25
    assert usage.data.cached_input_tokens == 4
    assert usage.data.cache_creation_tokens == 0
    assert usage.data.output_tokens == 12
    assert usage.data.reasoning_tokens == 0
    assert "reasoning_tokens" not in usage.data.known_token_fields
    assert usage.data.basis == "price_table"
    assert usage.data.reported_usd is None
    assert usage.data.cache_creation_by_ttl is None
    assert usage.data.coverage.completeness == "partial"
    assert usage.data.coverage.attribution_gap is False
    assert usage.data.coverage.observation_gap is False
    assert adapter.observation is not None
    summary = adapter.observation.usage
    assert summary.completeness == "partial"
    assert summary.observation_gap is True
    assert summary.attribution_gap is False
    assert len(summary.rows) == 1
    row = summary.rows[0]
    assert (
        row.input_tokens,
        row.cached_input_tokens,
        row.cache_creation_tokens,
        row.output_tokens,
    ) == (25, 4, 0, 12)
    assert row.completeness == "partial"
    assert row.reported_usd is None
    assert row.basis == "price_table"
    assert ended.data.subtype == "success"
    assert ended.data.is_error is False
    assert ended.data.terminal_reason == "completed"
    refuse_content(
        rendered_events(sink),
        ("hidden-delta", "hidden-result"),
        "session content reached an event",
    )
    assert_session_records(claude_capture.finish(), ("hidden-delta", "hidden-result"))


def test_claude_event_projection_refuses_a_non_integer_token_count() -> None:
    sink, events = projector()
    events.observe_init(init_fields())

    with pytest.raises(KoraProtocolError) as caught:
        events.usage_start(
            "msg-1",
            "claude-sonnet-4-5",
            None,
            {
                "input_tokens": True,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )

    error = caught.value
    assert error.code == "harness_error"
    assert error.message == "the Claude session failed"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert [event.kind for event in sink.events] == ["session_started"]


def test_claude_projection_keeps_calls_above_final_output() -> None:
    sink, events = projector()
    events.observe_init(init_fields())
    events.usage_start(
        "msg-1",
        "claude-sonnet-4-5",
        None,
        {
            "input_tokens": 25,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 0,
        },
    )
    events.usage_delta({"output_tokens": 12, "reasoning_tokens": 9})

    events.observe_result(
        "success",
        False,
        "completed",
        {
            "claude-sonnet-4-5": {
                "inputTokens": 30,
                "outputTokens": 7,
                "cacheReadInputTokens": 1,
                "cacheCreationInputTokens": 2,
            }
        },
    )
    events.finish()

    usage = sink.events[1]
    ended = sink.events[2]
    assert isinstance(usage, UsageEvent)
    assert isinstance(ended, ResultEvent)
    assert [event.kind for event in sink.events] == [
        "session_started",
        "usage",
        "result",
    ]
    assert usage.data.output_tokens == 12
    assert usage.data.reasoning_tokens == 9
    assert "reasoning_tokens" in usage.data.known_token_fields
    assert usage.data.coverage.completeness == "partial"
    assert ended.data.subtype == "success"
    assert ended.data.terminal_reason == "completed"

    summary = events.observation().usage
    assert summary.completeness == "partial"
    assert summary.observation_gap is True
    assert summary.rows[0].output_tokens == 12
    assert summary.rows[0].reasoning_tokens == 9
    assert "reasoning_tokens" in summary.rows[0].known_token_fields


def test_claude_event_projection_keeps_reasoning_inside_the_final_output() -> None:
    sink, events = projector()
    events.observe_init(init_fields())
    events.usage_start(
        "msg-1",
        "claude-sonnet-4-5",
        None,
        {
            "input_tokens": 25,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 0,
        },
    )
    events.usage_delta({"output_tokens": 12, "reasoning_tokens": 4})

    events.observe_result(
        "success",
        False,
        None,
        {
            "claude-sonnet-4-5": {
                "inputTokens": 25,
                "outputTokens": 12,
                "cacheReadInputTokens": 4,
                "cacheCreationInputTokens": 0,
            }
        },
    )
    events.finish()

    usage = sink.events[1]
    assert isinstance(usage, UsageEvent)
    assert usage.data.output_tokens == 12
    assert usage.data.reasoning_tokens == 4
    assert "reasoning_tokens" in usage.data.known_token_fields
    assert usage.data.coverage.completeness == "partial"

    summary = events.observation().usage
    assert summary.completeness == "complete"
    assert summary.observation_gap is False
    assert summary.rows[0].output_tokens == 12
    assert summary.rows[0].reasoning_tokens == 4
    assert "reasoning_tokens" in summary.rows[0].known_token_fields


def test_claude_event_projection_refuses_a_non_integer_final_token_count() -> None:
    sink, events = projector()
    events.observe_init(init_fields())
    events.usage_start(
        "msg-1",
        "claude-sonnet-4-5",
        None,
        {
            "input_tokens": 25,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 0,
        },
    )

    with pytest.raises(KoraProtocolError) as caught:
        events.observe_result(
            "success",
            False,
            "completed",
            {
                "claude-sonnet-4-5": {
                    "inputTokens": True,
                    "outputTokens": 7,
                    "cacheReadInputTokens": 1,
                    "cacheCreationInputTokens": 2,
                }
            },
        )

    error = caught.value
    assert error.code == "harness_error"
    assert error.message == "the Claude session failed"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert [event.kind for event in sink.events] == ["session_started", "usage"]
    assert events.observation().usage.rows[0].input_tokens == 25
    assert events.observation().usage.completeness == "partial"


def test_claude_event_projection_refuses_an_unknown_api_key_source() -> None:
    sink, events = projector()

    with pytest.raises(KoraProtocolError) as caught:
        events.observe_init(init_fields("apiKeyHelper"))

    error = caught.value
    assert error.code == "harness_error"
    assert error.message == "the Claude session failed"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert sink.events == []


@pytest.mark.parametrize(
    "text",
    [
        "NaN",
        "Infinity",
        "1e400",
        '{"key":1,"key":2}',
        "```json\n{}\n```",
        "answer {}",
        "",
        " ",
    ],
)
def test_result_text_requires_strict_json(text: str) -> None:
    _sink, events = projector()
    events.observe_init(init_fields())

    events.observe_result("success", False, None, None, result=text)

    assert events.result_text is None


@pytest.mark.parametrize("text", ["null", "false", "0", '"text"', "[]", "{}"])
def test_result_text_accepts_each_json_value_kind(text: str) -> None:
    _sink, events = projector()
    events.observe_init(init_fields())

    events.observe_result("success", False, None, None, result=text)

    assert events.result_text is not None
    assert events.result_text.value == json.loads(text)


@pytest.mark.parametrize("value", [float("nan"), json.loads("[" * 18 + "0" + "]" * 18)])
def test_structured_output_refuses_unrepresentable_values(value: object) -> None:
    _sink, events = projector()
    events.observe_init(init_fields())

    events.observe_result("success", False, None, None, structured_output=value)

    assert events.output_refused
    assert events.structured is None
