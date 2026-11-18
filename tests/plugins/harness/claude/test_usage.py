import json
from collections.abc import Generator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import utc_now
from kora.conformance.harness import ConformanceSink
from kora.ir import ErrorClass
from kora.ir.config import NodeLimits
from kora.ir.cost import MAX_TOKEN, UsageRow
from kora.observability import LoggingSettings
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.claude.events import ClaudeEvents
from kora.plugins.harness.claude.session import COMPONENT
from kora.plugins.harness.claude.usage import ClaudeUsage, UsageSummary, row_counts
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import UsageData, UsageEvent
from kora.runner.usage import MAX_CLOSED_MESSAGES, MAX_MODELS, MAX_OPEN_MESSAGES
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    WHEN,
    FrozenClock,
    assert_session_records,
    context_for,
    init_fields,
    projector,
)
from tests.plugins.harness.claude.test_session_lifecycle import (
    StoppingSink,
    oversized_recording,
    run_lifecycle,
    session_spec,
)

RECORDINGS = Path(__file__).resolve().parent / "recordings"
MODEL = "claude-sonnet-4-5"
SECOND_MODEL = "claude-haiku-4-5"
PENDING_LEGS = {
    "usd-counters": "#199 runner harness-host integration",
    "unpriced-model-usd": "#199 runner harness-host integration",
    "budget-exhausted": "#1388 deferred USD-budget feature of first-slice.md",
}
REPLAY_LEGS = (
    "fresh",
    "multi-model",
    "subagent",
    "aborted-tools",
    "aborted-streaming",
    "post-result",
    "cut-in-flight",
    "empty-model-usage",
    "nothing",
    "cost-list",
    "subscription",
    "ttl",
)
PROJECTOR_LEGS = (
    "duplicates",
    "redelivered-after-stop",
    "conflicting-start",
    "orphan-delta",
    "oversized-cut",
    "missing-model",
    "regressive-final",
    "unknown-model",
    "unknown-reasoning",
    "missing-counts",
    "ttl-unequal",
    "ttl-unattributable",
    "ttl-multiple-models",
    "ttl-missing-query-count",
    "ttl-inconsistent",
    "tail-new-model",
    "tail-empty-result",
    "tail-unknown-reasoning",
    "stable-source-time",
    "token-sum-overflow",
    "tail-sum-overflow",
    "missing-model-usage",
    "invalid-result-atomicity",
    "invalid-result-status",
    "invalid-result-denials",
    "final-presence",
    "reasoning-unequal",
)
MONEY_CASES: tuple[tuple[str, object, str | None, bool, str | None], ...] = (
    ("cost-integer", 2, "list", False, "2"),
    ("cost-zero", 0, "list", False, "0"),
    ("cost-fraction", 0.12345678905, "list", False, "0.123456789"),
    ("cost-absent-basis", 1.25, None, False, None),
    ("cost-other-basis", 1.25, "managed", False, None),
    ("cost-subscription", 2, "list", True, None),
)
INVALID_NUMBERS: tuple[tuple[str, object], ...] = (
    ("boolean", True),
    ("negative", -1),
    ("nan", float("nan")),
    ("infinite", float("inf")),
    ("overflow", 100_000_000_000_000),
    ("string", "1"),
)
INVALID_TOKENS: tuple[tuple[str, object], ...] = (
    ("boolean", True),
    ("float", 1.5),
    ("string", "1"),
    ("negative", -1),
    ("overflow", MAX_TOKEN + 1),
)
INVALID_SHAPES: tuple[tuple[str, object], ...] = (
    ("non-dict-model-usage", []),
    ("non-dict-entry", {MODEL: []}),
    ("invalid-model", {"": {}}),
    ("non-string-model", {1: {}}),
    ("oversized-identity", {"m" * 245: {}}),
    ("oversized-source", {MODEL: {"payload": "x" * 8192}}),
)
CASES = (
    *REPLAY_LEGS,
    *PROJECTOR_LEGS,
    *(case[0] for case in MONEY_CASES),
    *(f"cost-invalid-{name}" for name, _ in INVALID_NUMBERS),
    *(
        f"{place}-invalid-{name}"
        for place in ("start", "delta", "final")
        for name, _ in INVALID_TOKENS
    ),
    *(name for name, _ in INVALID_SHAPES),
)
SECTION_13_LEGS = frozenset(
    {
        "fresh",
        "multi-model",
        "subagent",
        "aborted-tools",
        "aborted-streaming",
        "post-result",
        "cut-in-flight",
        "empty-model-usage",
        "nothing",
        "cost-list",
        "subscription",
        "ttl",
        "duplicates",
        "redelivered-after-stop",
        "conflicting-start",
        "orphan-delta",
        "oversized-cut",
        "missing-model",
        "regressive-final",
        "unknown-model",
        "unknown-reasoning",
        "missing-counts",
        "ttl-unequal",
        "ttl-unattributable",
        "ttl-multiple-models",
        "ttl-missing-query-count",
        "ttl-inconsistent",
        "tail-new-model",
        "tail-empty-result",
        "tail-unknown-reasoning",
        "stable-source-time",
        "token-sum-overflow",
        "tail-sum-overflow",
        "missing-model-usage",
        "invalid-result-atomicity",
        "invalid-result-status",
        "invalid-result-denials",
        "final-presence",
        "reasoning-unequal",
        "cost-integer",
        "cost-zero",
        "cost-fraction",
        "cost-absent-basis",
        "cost-other-basis",
        "cost-subscription",
        "cost-invalid-boolean",
        "cost-invalid-negative",
        "cost-invalid-nan",
        "cost-invalid-infinite",
        "cost-invalid-overflow",
        "cost-invalid-string",
        "start-invalid-boolean",
        "start-invalid-float",
        "start-invalid-string",
        "start-invalid-negative",
        "start-invalid-overflow",
        "delta-invalid-boolean",
        "delta-invalid-float",
        "delta-invalid-string",
        "delta-invalid-negative",
        "delta-invalid-overflow",
        "final-invalid-boolean",
        "final-invalid-float",
        "final-invalid-string",
        "final-invalid-negative",
        "final-invalid-overflow",
        "non-dict-model-usage",
        "non-dict-entry",
        "invalid-model",
        "non-string-model",
        "oversized-identity",
        "oversized-source",
        "usd-counters",
        "unpriced-model-usd",
        "budget-exhausted",
        "adapter-budget-refusal",
        "oversized-replay-observation",
    }
)


@pytest.fixture
def usage_capture() -> Generator[Capture]:
    with captured(COMPONENT, LoggingSettings(), "claude-usage", utc_now) as capture:
        yield capture


def entry() -> dict[str, object]:
    return {
        "inputTokens": 10,
        "cacheReadInputTokens": 2,
        "cacheCreationInputTokens": 3,
        "outputTokens": 4,
    }


def query() -> dict[str, object]:
    return {
        "input_tokens": 10,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 3,
        "output_tokens": 4,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1,
            "ephemeral_1h_input_tokens": 2,
        },
    }


def start(
    events: ClaudeEvents,
    identity: str = "call",
    model: str | None = MODEL,
    scope: str | None = None,
) -> None:
    events.usage_start(
        identity,
        model,
        scope,
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 3,
        },
    )


def result(events: ClaudeEvents, rows: object, usage: object = None) -> None:
    events.observe_result("success", False, "completed", rows, query_usage=usage)


def usages(sink: ConformanceSink) -> tuple[UsageData, ...]:
    return tuple(event.data for event in sink.events if isinstance(event, UsageEvent))


def assert_tokens(row: UsageRow, expected: tuple[int, int, int, int, int]) -> None:
    assert tuple(row_counts(row).values()) == expected


def assert_recording(leg: str, summary: UsageSummary, sink: ConformanceSink) -> None:
    calls = usages(sink)
    assert all(call.coverage.completeness == "partial" for call in calls)
    assert len({call.message_id for call in calls}) == len(calls)
    assert summary.attribution_gap is False
    assert summary.observation_gap is False
    assert summary.observed_at == (None if leg == "nothing" else WHEN)
    assert summary.completeness == (
        "unavailable"
        if leg == "nothing"
        else "partial"
        if leg in {"aborted-streaming", "cut-in-flight", "empty-model-usage"}
        else "complete"
    )
    if leg == "nothing":
        assert calls == ()
        assert summary.rows == ()
        return
    assert len(calls) == 2
    assert [(call.input_tokens, call.output_tokens) for call in calls[:2]] == [
        (10, 4),
        (20, 0 if leg == "cut-in-flight" else 6),
    ]
    rows = {row.model: row for row in summary.rows}
    if leg == "multi-model":
        assert set(rows) == {MODEL, SECOND_MODEL}
        assert_tokens(rows[MODEL], (10, 2, 1, 4, 1))
        assert_tokens(rows[SECOND_MODEL], (20, 3, 2, 6, 2))
    else:
        assert set(rows) == {MODEL}
        assert_tokens(
            rows[MODEL],
            (30, 5, 3, 4, 1) if leg == "cut-in-flight" else (30, 5, 3, 10, 3),
        )
    row = rows[MODEL]
    assert ("reasoning_tokens" in row.known_token_fields) == (leg != "cut-in-flight")
    assert row.completeness == summary.completeness
    assert row.cache_creation_by_ttl == ({"5m": 1, "1h": 2} if leg == "ttl" else None)
    assert row.reported_usd == (
        "0.123456789" if leg in {"cost-list", "post-result"} else None
    )
    assert row.basis == (
        "harness_list" if leg in {"cost-list", "post-result"} else "price_table"
    )
    assert row.billing_class == ("subscription" if leg == "subscription" else "api_key")
    if leg == "subagent":
        assert [call.agent_id for call in calls] == [None, "tool-agent"]
    if leg not in {"cut-in-flight", "empty-model-usage"}:
        recorded = [
            cast("dict[str, object]", json.loads(line))
            for line in (RECORDINGS / f"constructed-usage-{leg}-2.1.301.ndjson")
            .read_text()
            .splitlines()
        ]
        final = next(item for item in recorded if item["type"] == "result")
        models = cast("dict[str, dict[str, object]]", final["modelUsage"])
        assert all(dict(item.source) == models[item.model] for item in summary.rows)
        assert row.observation_id == f"model-usage:{MODEL}"
        assert row.observation_sequence == 2
        if leg == "fresh":
            final_query = cast("dict[str, int]", final["usage"])
            assistants = [
                cast("dict[str, dict[str, int]]", item["message"])
                for item in recorded
                if item["type"] == "assistant"
            ]
            assert (
                sum(call.input_tokens for call in calls) == final_query["input_tokens"]
            )
            assert (
                sum(call.output_tokens for call in calls)
                == final_query["output_tokens"]
            )
            assert (
                sum(item["usage"]["input_tokens"] for item in assistants)
                != final_query["input_tokens"]
            )
            assert (
                sum(item["usage"]["output_tokens"] for item in assistants)
                != final_query["output_tokens"]
            )


def exercise_projector(leg: str) -> None:
    sink, events = projector()
    events.observe_init(init_fields())
    if leg == "stable-source-time":
        times = iter((WHEN, WHEN + timedelta(seconds=1), WHEN + timedelta(seconds=2)))
        events = ClaudeEvents(
            sink,
            SecretRegistry(),
            binding="claude-test",
            max_retries=2,
            output_format=False,
            check_deadline=lambda: None,
            observed_at=lambda: next(times),
        )
        events.observe_init(init_fields())
        start(events)
        events.usage_delta({"output_tokens": 4})

        result(events, {MODEL: entry()})
        first = events.observation().usage
        repeated = events.observation().usage

        assert first == repeated
        assert first.observed_at == WHEN + timedelta(seconds=1)
        assert first.rows[0].observed_at == first.observed_at
        assert next(times) == WHEN + timedelta(seconds=2)
        return
    if leg == "final-presence":
        result(events, {MODEL: {"inputTokens": 0}})

        summary = events.observation().usage

        assert summary.completeness == "complete"
        assert summary.rows[0].known_token_fields == {"input_tokens"}
        assert_tokens(summary.rows[0], (0, 0, 0, 0, 0))
        return
    start(
        events,
        model=None if leg == "unknown-model" else MODEL,
        scope="agent" if leg == "ttl-unattributable" else None,
    )
    scope = "agent" if leg == "ttl-unattributable" else None
    events.usage_delta({"output_tokens": 4, "reasoning_tokens": 1}, scope)
    final = entry()
    raw_query = query()

    if leg.startswith("invalid-result-"):
        malformed = entry() | {"inputTokens": True}
        rejected = (
            {SECOND_MODEL: entry(), MODEL: malformed}
            if leg == "invalid-result-atomicity"
            else {MODEL: entry()}
        )
        with pytest.raises(KoraProtocolError):
            events.observe_result(
                "success",
                False,
                "completed",
                rejected,
                api_error_status=True if leg == "invalid-result-status" else None,
                permission_denials={} if leg == "invalid-result-denials" else None,
            )
        fallback = events.observation().usage
        result(events, {MODEL: entry()})
        accepted = events.observation().usage

        assert fallback.completeness == "partial"
        assert len(fallback.rows) == 1
        assert fallback.rows[0].model == MODEL
        assert_tokens(fallback.rows[0], (10, 2, 3, 4, 1))
        assert accepted.completeness == "complete"
        assert accepted.rows[0].observation_sequence == 1
        return
    if leg in {"duplicates", "conflicting-start", "redelivered-after-stop"}:
        events.usage_start(
            "call",
            MODEL,
            None,
            {
                "input_tokens": 11 if leg == "conflicting-start" else 10,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 3,
            },
        )
        events.usage_delta({"output_tokens": 4, "reasoning_tokens": 1})
        events.usage_stop()
        start(events)
    if leg == "redelivered-after-stop":
        events.usage_delta({"output_tokens": 4, "reasoning_tokens": 1})
        events.usage_stop()
    if leg == "orphan-delta":
        events.usage_delta({"output_tokens": 999}, "absent")
    if leg == "unknown-reasoning":
        events.usage_delta({"output_tokens": 4})
    if leg == "reasoning-unequal":
        final["outputTokens"] = 5
    if leg == "regressive-final":
        final["outputTokens"] = 3
    if leg == "missing-counts":
        final.pop("cacheReadInputTokens")
        final["outputTokens"] = 5
    if leg == "ttl-unequal":
        raw_query["cache_creation"] = {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 3,
        }
    if leg == "ttl-missing-query-count":
        raw_query.pop("cache_read_input_tokens")
    if leg == "ttl-inconsistent":
        raw_query["cache_creation"] = {
            "ephemeral_5m_input_tokens": 1,
            "ephemeral_1h_input_tokens": 3,
        }
        with pytest.raises(KoraProtocolError) as caught:
            result(events, {MODEL: final}, raw_query)
        assert caught.value.message == "the Claude session failed"
        assert caught.value.__context__ is None
        return
    if leg in {"token-sum-overflow", "tail-sum-overflow"}:
        events.usage_stop()
        if leg == "tail-sum-overflow":
            final["inputTokens"] = MAX_TOKEN
            result(events, {MODEL: final})
        events.usage_start("huge", MODEL, None, {"input_tokens": MAX_TOKEN})
    elif leg not in {"oversized-cut", "stable-source-time"}:
        models = {SECOND_MODEL: final} if leg == "missing-model" else {MODEL: final}
        if leg == "ttl-multiple-models":
            models[SECOND_MODEL] = entry()
        result(
            events,
            None
            if leg == "missing-model-usage"
            else {}
            if leg == "tail-empty-result"
            else models,
            raw_query,
        )
    if leg.startswith("tail-") and leg != "tail-sum-overflow":
        start(events, "tail", SECOND_MODEL if leg == "tail-new-model" else MODEL)
        events.usage_delta(
            {"output_tokens": 4}
            if leg == "tail-unknown-reasoning"
            else {"output_tokens": 4, "reasoning_tokens": 1}
        )
    events.finish()
    summary = events.observation().usage

    assert events.observation().usage == summary
    assert summary.observed_at == WHEN
    calls = usages(sink)
    if leg in {"duplicates", "conflicting-start", "redelivered-after-stop"}:
        assert len(calls) == 1
        assert calls[0].input_tokens == 10
    if leg == "token-sum-overflow":
        assert summary.rows == ()
        assert summary.completeness == "unavailable"
        assert summary.observation_gap is True
        return
    row = next((item for item in summary.rows if item.model == MODEL), summary.rows[0])
    assert summary.observation_gap == (
        leg
        in {
            "conflicting-start",
            "orphan-delta",
            "regressive-final",
            "missing-model",
            "missing-counts",
            "tail-sum-overflow",
        }
    )
    assert summary.attribution_gap == (leg == "unknown-model")
    partial = leg in {
        "conflicting-start",
        "orphan-delta",
        "regressive-final",
        "missing-model",
        "missing-counts",
        "unknown-model",
        "oversized-cut",
        "stable-source-time",
        "missing-model-usage",
    } or leg.startswith("tail-")
    assert summary.completeness == ("partial" if partial else "complete")
    assert row.output_tokens == (
        8
        if leg in {"tail-empty-result", "tail-unknown-reasoning"}
        else 5
        if leg == "reasoning-unequal"
        else 4
    )
    assert row.input_tokens == (
        MAX_TOKEN
        if leg == "tail-sum-overflow"
        else 20
        if leg in {"tail-empty-result", "tail-unknown-reasoning"}
        else 10
    )
    if leg == "tail-new-model":
        assert len(summary.rows) == 2
    if leg in {
        "unknown-reasoning",
        "reasoning-unequal",
        "unknown-model",
        "tail-unknown-reasoning",
        "tail-sum-overflow",
    }:
        assert "reasoning_tokens" not in row.known_token_fields
    if leg in {"ttl-unattributable", "ttl-multiple-models", "ttl-missing-query-count"}:
        assert row.cache_creation_by_ttl is None
    if leg == "ttl-unequal":
        assert row.cache_creation_by_ttl == {"5m": 0, "1h": 3}


def exercise_ingress(leg: str) -> None:
    sink, events = projector()
    money_case = next((case for case in MONEY_CASES if case[0] == leg), None)
    events.observe_init(
        init_fields("none" if money_case and money_case[3] else "ANTHROPIC_API_KEY")
    )
    final = entry()
    if money_case is not None:
        final["costUSD"] = money_case[1]
        if money_case[2] is not None:
            final["costBasis"] = money_case[2]

        result(events, {MODEL: final})
        summary = events.observation().usage

        assert summary.completeness == "complete"
        assert summary.rows[0].reported_usd == money_case[4]
        assert summary.rows[0].basis == (
            "harness_list"
            if money_case[2] == "list" and not money_case[3]
            else "price_table"
        )
        assert dict(summary.rows[0].source) == final
        assert usages(sink) == ()
        return
    value: object = None
    if leg.startswith("cost-invalid-"):
        value = dict(INVALID_NUMBERS)[leg.removeprefix("cost-invalid-")]
        final["costUSD"] = value
    elif "-invalid-" in leg:
        value = dict(INVALID_TOKENS)[leg.split("-invalid-")[1]]
        final["inputTokens"] = value
    elif leg in dict(INVALID_SHAPES):
        value = dict(INVALID_SHAPES)[leg]
    if leg.startswith("delta-invalid-"):
        start(events)

    def admit_invalid() -> None:
        if leg.startswith("start-invalid-"):
            events.usage_start("call", MODEL, None, {"input_tokens": value})
        elif leg.startswith("delta-invalid-"):
            events.usage_delta({"output_tokens": value})
        else:
            result(events, value if leg in dict(INVALID_SHAPES) else {MODEL: final})

    with pytest.raises(KoraProtocolError) as caught:
        admit_invalid()

    assert caught.value.code == "harness_error"
    assert caught.value.message == "the Claude session failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert usages(sink) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", CASES, ids=CASES)
async def test_claude_usage_from_stream_events(
    leg: str, usage_capture: Capture, tmp_path: Path
) -> None:
    if leg in PROJECTOR_LEGS:
        exercise_projector(leg)
        return
    if leg not in REPLAY_LEGS:
        exercise_ingress(leg)
        return
    sink = StoppingSink()

    _result, outcome, adapter = await run_lifecycle(
        usage_capture,
        "usage",
        sink=sink,
        clock=FrozenClock(),
        recording=RECORDINGS / f"constructed-usage-{leg}-2.1.301.ndjson",
    )

    assert adapter.observation is not None
    assert_recording(leg, adapter.observation.usage, sink)
    expected_cause = (
        "cli_exit_without_result"
        if leg in {"cut-in-flight", "nothing"}
        else "completed"
    )
    assert outcome.cause == expected_cause
    assert_session_records(usage_capture.finish(), ("hello",), expected_cause)


def test_usage_legs_cover_the_section_13_row() -> None:
    covered = frozenset(CASES) | {
        "adapter-budget-refusal",
        "oversized-replay-observation",
    }
    pending = frozenset(PENDING_LEGS)

    combined = covered | pending

    assert combined == SECTION_13_LEGS
    assert covered.isdisjoint(pending)
    assert all(owner.startswith("#") for owner in PENDING_LEGS.values())


@dataclass
class CountingSink:
    count: int = 0

    def emit(self, data: UsageData) -> None:
        self.count += 1


def bounded_usage() -> tuple[CountingSink, ClaudeUsage]:
    sink = CountingSink()
    usage = ClaudeUsage(sink.emit, lambda: sink.count, lambda: WHEN)
    usage.billing = "api_key"
    return sink, usage


@pytest.mark.parametrize("dimension", ["scopes", "ids", "models", "model-union"])
def test_claude_usage_state_stays_bounded(dimension: str) -> None:
    sink, usage = bounded_usage()
    bound = MAX_MODELS if dimension in {"models", "model-union"} else MAX_OPEN_MESSAGES

    for index in range(bound + 2):
        usage.start(
            f"call-{index}",
            f"model-{index}" if dimension in {"models", "model-union"} else MODEL,
            f"scope-{index}" if dimension == "scopes" else None,
            {"input_tokens": 1},
        )
        if dimension != "scopes":
            usage.stop()
    if dimension == "model-union":
        usage.result({"final-model": entry()}, None, "completed")
        usage.start("tail", "tail-model", None, {"input_tokens": 1})
    summary = usage.summary()
    count_before_duplicate = sink.count
    usage.start(
        "call-0",
        "model-0" if dimension in {"models", "model-union"} else MODEL,
        "scope-0" if dimension == "scopes" else None,
        {"input_tokens": 1},
    )
    repeated = usage.summary()

    assert repeated == summary
    assert sink.count == count_before_duplicate
    assert (
        summary.attribution_gap
        if dimension in {"models", "model-union"}
        else summary.observation_gap
    )
    assert summary.completeness != "complete"
    assert len(usage.open) <= MAX_OPEN_MESSAGES
    assert len(usage.closed) <= MAX_CLOSED_MESSAGES
    assert len(usage.models) <= MAX_MODELS
    assert len(usage.final) <= MAX_MODELS
    assert len(summary.rows) <= MAX_MODELS
    for ledger in (usage.ledger, usage.tail):
        assert len(ledger.models) <= MAX_MODELS
        assert len(ledger.closed) <= MAX_MODELS
        assert len(ledger.closed_ids) <= MAX_CLOSED_MESSAGES
        assert len(ledger.open) == 0


def test_adapter_budget_refusal(usage_capture: Capture, tmp_path: Path) -> None:

    adapter = make_adapter(tmp_path, context_for(usage_capture, FrozenClock()))

    with pytest.raises(KoraProtocolError) as caught:
        session_spec(adapter, NodeLimits(budget_usd=1))

    assert caught.value.code == "config_error"
    assert caught.value.details == {}


@pytest.mark.asyncio
async def test_oversized_replay_observation(
    usage_capture: Capture, tmp_path: Path
) -> None:

    recording = oversized_recording(tmp_path)

    result, outcome, _adapter = await run_lifecycle(
        usage_capture, "usage", recording=recording
    )

    assert result.error_class is ErrorClass.HARNESS_ERROR
    assert outcome.cause == "oversized_message"
    assert outcome.termination == "killed"
    assert result.usage_coverage.completeness == "partial"
    assert result.usage[0].input_tokens == 10
