import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal

import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from pydantic import ValidationError

from kora.conformance.capture import Capture
from kora.conformance.harness import ConformancePolicy, Trace
from kora.ir.json import JsonObject
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.harness.claude.events import ClaudeEvents
from kora.plugins.harness.claude.sdk import feed
from kora.plugins.harness.claude.signals import MAX_LIMIT_EVENTS, ProviderSignals
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EVENT_ADAPTER,
    ProviderLimitedData,
    ProviderLimitedEvent,
    ProviderRetryingData,
    ProviderRetryingEvent,
    SessionResult,
)
from kora.runner.sink import RunnerSink
from tests.plugins.harness.claude import recording_builder as frames
from tests.plugins.harness.claude.conformance_launch import RECORDINGS, ClaudeLaunches
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    WHEN,
    FrozenClock,
    context_for,
)
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_session_lifecycle import session_spec
from tests.plugins.harness.claude.test_stream_volume import MemoryJournal, close_sink
from tests.runner.agent_support import SteppedClock

BINDING = "provider-test-binding"
MAX_RETRIES = 2


async def signal_run(
    capture: Capture, recording: Path, max_retries: int = MAX_RETRIES
) -> tuple[list[ProviderLimitedData | ProviderRetryingData], SessionResult]:
    launches = ClaudeLaunches(Path(mkdtemp(prefix="kc", dir="/tmp")), recording)
    clock = FrozenClock()
    adapter = make_adapter(
        launches.workspace,
        context_for(capture, clock),
        binding=BINDING,
        max_retries=max_retries,
    )
    journal = MemoryJournal()
    sink = RunnerSink(journal, adapter.registry, clock, None, 900_000_000_000)
    try:
        async with asyncio.timeout(10):
            result = await adapter.run(
                session_spec(adapter),
                ConformancePolicy("Forbidden", Trace()),
                sink,
                launches,
            )
    finally:
        await launches.close()
        await close_sink(sink, clock)
    signals: list[ProviderLimitedData | ProviderRetryingData] = []
    for index, (kind, payload) in enumerate(journal.records):
        if kind not in {"provider_limited", "provider_retrying"}:
            continue
        event = EVENT_ADAPTER.validate_python(
            {"i": index, "at": WHEN, "kind": kind, "data": json.loads(payload)}
        )
        assert isinstance(event, ProviderLimitedEvent | ProviderRetryingEvent)
        signals.append(event.data)
    return signals, result


def limit(
    scope: Literal["rate", "quota"] = "quota",
    group: str | None = "five_hour",
    reset: int | None = 123,
    model: str = frames.MODEL,
) -> ProviderLimitedData:
    return ProviderLimitedData(
        scope=scope,
        vendor_group=group,
        retry_after=None if reset is None else datetime.fromtimestamp(reset, UTC),
        model=model,
        binding=BINDING,
    )


def retry(
    attempt: int, delay: int | None, model: str = frames.MODEL
) -> ProviderRetryingData:
    return ProviderRetryingData(
        model=model,
        retry_number=attempt,
        retry_after=None if delay is None else WHEN + timedelta(milliseconds=delay),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rate-rejected", [limit()]),
        ("rate-allowed", []),
        ("rate_limit", [limit("rate", None, None)]),
        ("api-429", [limit("rate", None, None)]),
        ("limit-then-provider", [limit()]),
        ("rate-repeated", [limit()]),
        ("retry-success", [limit(), retry(1, 1)]),
        ("retry-capped", [retry(1, 100), retry(2, 200)]),
    ],
)
async def test_claude_provider_signals_are_normalised(
    claude_capture: Capture,
    name: str,
    expected: list[ProviderLimitedData | ProviderRetryingData],
) -> None:
    recording = RECORDINGS / f"constructed-{name}-2.1.301.ndjson"

    signals, result = await signal_run(claude_capture, recording)

    assert result.stop_reason == "completed"
    assert signals == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
async def test_claude_nonlimiting_rate_notices_refresh_liveness(
    status: Literal["allowed", "allowed_warning"],
) -> None:
    clock = SteppedClock()
    registry = SecretRegistry()
    journal = MemoryJournal()
    sink = RunnerSink(journal, registry, clock, 1, 1_000_000_000)
    events = ClaudeEvents(
        sink,
        registry,
        binding=BINDING,
        max_retries=MAX_RETRIES,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=clock.now,
    )
    notice = RateLimitEvent(RateLimitInfo(status), uuid="rate", session_id="session")
    activity_before = sink.liveness().last_event_ns

    try:
        await clock.advance_to(900_000_000)
        feed(events, notice)
        activity_after = sink.liveness().last_event_ns
        enforcement = sink.enforcement()
    finally:
        await close_sink(sink, clock)

    assert activity_before == 0
    assert activity_after == 900_000_000
    assert enforcement.action == "continue"
    assert journal.records == []


def rejected(reset: int = 123, group: str | None = "five_hour") -> JsonObject:
    frame = frames.rate_limit()
    frame["rate_limit_info"] = {
        "status": "rejected",
        "resetsAt": reset,
        "rateLimitType": group,
    }
    return frame


@pytest.mark.asyncio
async def test_provider_limits_keep_latest_model_and_bound_distinct_windows(
    claude_capture: Capture, tmp_path: Path
) -> None:
    model = "claude-opus-4-5"
    recording = frames.recording(
        tmp_path / "windows.ndjson",
        [
            frames.init_frame(),
            rejected(),
            frames.assistant("new-model", frames.text_block("discarded"), model=model),
            rejected(),
            *[rejected(reset) for reset in range(MAX_LIMIT_EVENTS + 5)],
            frames.assistant("synthetic", model="<synthetic>")
            | {"error": "rate_limit"},
            frames.success() | {"api_error_status": 429},
        ],
    )

    signals, result = await signal_run(claude_capture, recording)

    assert result.stop_reason == "completed"
    assert signals == [
        limit(),
        *[limit(reset=reset, model=model) for reset in range(MAX_LIMIT_EVENTS - 1)],
    ]


@pytest.mark.asyncio
async def test_retry_budget_resets_only_for_first_seen_assistant_ids(
    claude_capture: Capture, tmp_path: Path
) -> None:
    recording = frames.recording(
        tmp_path / "calls.ndjson",
        [
            frames.init_frame(),
            frames.assistant("one"),
            frames.api_retry(0),
            frames.api_retry(1, 0),
            frames.assistant("one"),
            frames.api_retry(2, 1),
            frames.assistant("synthetic", model="<synthetic>"),
            frames.api_retry(3, 1),
            frames.assistant("two", model="claude-opus-4-5"),
            frames.api_retry(2, 5),
            frames.api_retry(3, -1),
            frames.api_retry(4, 1),
            frames.assistant("one"),
            frames.api_retry(5, 1),
            frames.assistant("two", model="claude-opus-4-5"),
            frames.api_retry(6, 1),
            frames.success(),
        ],
    )

    signals, result = await signal_run(claude_capture, recording)

    assert result.stop_reason == "completed"
    assert signals == [
        retry(0, None),
        retry(1, 0),
        retry(2, 5, "claude-opus-4-5"),
        retry(3, None, "claude-opus-4-5"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        frames.api_retry() | {"attempt": True},
        frames.api_retry() | {"attempt": -1},
        frames.api_retry() | {"attempt": "1"},
        {"type": "system", "subtype": "api_retry"},
        rejected(10**30),
        rejected() | {"rate_limit_info": {"status": "rejected", "resetsAt": True}},
        rejected(group="has space"),
    ],
)
async def test_malformed_provider_signals_are_refused(
    claude_capture: Capture, tmp_path: Path, frame: JsonObject
) -> None:
    recording = frames.recording(
        tmp_path / "invalid.ndjson",
        [
            frames.init_frame(),
            frame,
            {"kora_replay": "await_interrupt"},
            frames.success(),
        ],
    )

    with pytest.raises(KoraProtocolError) as error:
        await signal_run(claude_capture, recording)

    assert error.value.code == "harness_error"
    assert error.value.message == "the Claude session failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal",
    [
        [rejected()],
        [frames.api_retry()],
        [frames.success() | {"api_error_status": 429}],
        [frames.assistant("one", model="bad\nmodel"), frames.api_retry()],
    ],
)
async def test_provider_signal_requires_a_known_model(
    claude_capture: Capture, tmp_path: Path, signal: list[JsonObject]
) -> None:
    init = frames.init_frame()
    del init["model"]
    recording = frames.recording(
        tmp_path / "missing-model.ndjson",
        [
            init,
            *signal,
            {"kora_replay": "await_interrupt"},
            frames.success(),
        ],
    )

    with pytest.raises(KoraProtocolError) as error:
        await signal_run(claude_capture, recording)

    assert error.value.code == "harness_error"
    assert error.value.message == "the Claude session failed"


@pytest.mark.parametrize("binding", [{}, {"binding": "has space"}, {"binding": None}])
def test_claude_environment_requires_valid_binding(binding: dict[str, object]) -> None:
    configuration = {
        "bash_path": "/bin/bash",
        "max_retries": 2,
        "cli_telemetry": False,
    } | binding

    with pytest.raises(ValidationError) as error:
        ClaudeEnvironmentConfiguration.model_validate(configuration)

    assert error.value.errors()[0]["loc"] == ("binding",)


def test_signal_budgets_count_dropped_frames_and_reopen_for_new_calls() -> None:
    dropped: list[str] = []
    signals = ProviderSignals(BINDING, MAX_RETRIES, FrozenClock().now, dropped.append)
    signals.assistant(True, frames.MODEL)

    limits = [
        signals.rejected(reset, "five_hour") for reset in range(MAX_LIMIT_EVENTS + 3)
    ]
    repeated = signals.rejected(MAX_LIMIT_EVENTS - 1, "five_hour")
    retries = [signals.retry({"attempt": attempt}) for attempt in range(5)]
    signals.assistant(False, frames.MODEL)
    same_call = signals.retry({"attempt": 5})
    signals.assistant(True, frames.MODEL)
    next_call = signals.retry({"attempt": 1, "retry_delay_ms": 10**30})

    assert limits == [
        *[limit(reset=reset) for reset in range(MAX_LIMIT_EVENTS)],
        None,
        None,
        None,
    ]
    assert repeated is None
    assert retries == [retry(0, None), retry(1, None), None, None, None]
    assert same_call is None
    assert next_call == retry(1, None)
    assert dropped == ["message"] * 4 + ["system"] * 4


@pytest.mark.asyncio
async def test_nonconsecutive_limits_and_missing_window_fields(
    claude_capture: Capture, tmp_path: Path
) -> None:
    windowless = frames.rate_limit()
    windowless["rate_limit_info"] = {"status": "rejected"}
    recording = frames.recording(
        tmp_path / "distinct.ndjson",
        [
            frames.init_frame(),
            rejected(),
            rejected(124),
            rejected(),
            windowless,
            frames.success(),
        ],
    )

    signals, result = await signal_run(claude_capture, recording)

    assert result.stop_reason == "completed"
    assert signals == [
        limit(),
        limit(reset=124),
        limit(),
        limit(group=None, reset=None),
    ]


@pytest.mark.asyncio
async def test_zero_retry_budget_emits_no_retries(claude_capture: Capture) -> None:
    recording = RECORDINGS / "constructed-retry-capped-2.1.301.ndjson"

    signals, result = await signal_run(claude_capture, recording, max_retries=0)

    assert result.stop_reason == "completed"
    assert signals == []


@pytest.mark.asyncio
async def test_quota_limit_suppresses_later_assistant_and_result_rate_signals(
    claude_capture: Capture, tmp_path: Path
) -> None:
    recording = frames.recording(
        tmp_path / "fallbacks.ndjson",
        [
            frames.init_frame(),
            rejected(),
            frames.assistant("rate", model="claude-opus-4-5") | {"error": "rate_limit"},
            frames.success() | {"api_error_status": 429},
        ],
    )

    signals, result = await signal_run(claude_capture, recording)

    assert result.stop_reason == "completed"
    assert signals == [limit()]
