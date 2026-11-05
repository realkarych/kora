import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from kora.conformance.harness import ConformanceSink, Trace
from kora.ir.cost import MAX_SUM, MAX_TOKEN
from kora.plugins.harness.codex.app_server_rpc import Notification, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_session import AppServerTurnSession
from kora.plugins.harness.codex.app_server_usage import (
    ConfirmedUsage,
    TokenCounts,
    UsageError,
    UsageFailure,
    UsageObserver,
)
from kora.plugins.harness.codex.usage_events import (
    CodexUsageEvents,
    UsageEventFailure,
    UsageEventRefused,
    UsageStamp,
)
from kora.protocols.harness import UsageData, UsageEvent
from kora.runner.usage import UsageLedger
from tests.plugins.harness.codex.test_app_server_turn import completed, frame, started
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

THREAD = "native-private-thread-canary"
TURN = "native printable content canary"
MESSAGE = "kora-owned-turn"
TIME = datetime(2026, 11, 6, tzinfo=UTC)
SECOND = timedelta(seconds=1)
FIRST = TokenCounts(100, 20, 30, 10, 4)
ADVANCED = TokenCounts(160, 25, 40, 30, 10)
LATEST = TokenCounts(190, 28, 42, 35, 12)


def usage_frame(
    counts: TokenCounts, *, turn: str = TURN, total_tokens: int | None = None
) -> bytes:
    total = {
        "inputTokens": counts.native_input_tokens,
        "cachedInputTokens": counts.cached_input_tokens,
        "outputTokens": counts.output_tokens,
        "reasoningOutputTokens": counts.reasoning_tokens,
        "totalTokens": (
            counts.native_input_tokens + counts.output_tokens
            if total_tokens is None
            else total_tokens
        ),
    }
    if counts.cache_creation_tokens is not None:
        total["cacheWriteInputTokens"] = counts.cache_creation_tokens
    return frame(
        {
            "method": "thread/tokenUsage/updated",
            "emittedAtMs": 4_242_424,
            "params": {
                "threadId": THREAD,
                "turnId": turn,
                "tokenUsage": {"total": total, "last": total},
            },
        }
    )


class ReceiptClock:
    def __init__(self) -> None:
        self.at: datetime = TIME
        self.reads: int = 0

    def now(self) -> datetime:
        self.reads += 1
        return self.at


class Stamps:
    def __init__(self) -> None:
        self.calls: int = 0

    def next(self) -> UsageStamp:
        self.calls += 1
        return UsageStamp(f"usage-{self.calls}", self.calls)


def data_from(sink: ConformanceSink) -> list[UsageData]:
    result: list[UsageData] = []
    for event in sink.events:
        assert isinstance(event, UsageEvent)
        result.append(event.data)
    return result


@pytest.mark.asyncio
async def test_codex_usage_events_preserve_source_time_and_unattributed_counts() -> (
    None
):
    sink = ConformanceSink(None, Trace())
    clock = ReceiptClock()
    stamps = Stamps()
    emitter = CodexUsageEvents(sink, message_id=MESSAGE, next_stamp=stamps.next)
    session = AppServerTurnSession(THREAD, max_messages=2, max_tools=2)
    received: asyncio.Queue[None] = asyncio.Queue()
    confirmed = asyncio.Event()

    def observed(notification: Notification) -> None:
        assert notification.emitted_at_ms == 4_242_424
        received.put_nowait(None)

    def turn_confirmed(turn: str) -> None:
        assert turn == TURN
        assert session.snapshot().turn_id == TURN
        confirmed.set()

    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt="fixture",
                deadline=future_deadline(),
                usage_observer=UsageObserver(clock.now, emitter.observe),
                observe=observed,
                on_started=turn_confirmed,
            )
        )
        await connection.peer_reader.readline()

        await connection.send(usage_frame(FIRST))
        await received.get()
        clock.at = TIME + SECOND
        await connection.send(usage_frame(ADVANCED))
        await received.get()
        before_reply = (len(sink.events), stamps.calls, clock.reads)
        clock.at = TIME + 10 * SECOND
        await connection.send(started({"turn": {"id": TURN, "status": "inProgress"}}))
        await confirmed.wait()
        await connection.send(usage_frame(ADVANCED))
        await received.get()
        await connection.send(usage_frame(FIRST))
        await received.get()
        await connection.send(usage_frame(FIRST))
        await received.get()
        clock.at = TIME + 20 * SECOND
        await connection.send(usage_frame(LATEST))
        await received.get()
        await connection.send(completed(thread=THREAD, turn=TURN))
        terminal = await operation

    observed_data = data_from(sink)
    ledger = UsageLedger()
    for data in observed_data:
        ledger.observe(data)
    rows = ledger.rows()
    coverage = ledger.coverage(result_seen=True)

    assert terminal.status == "completed"
    assert before_reply == (0, 0, 2)
    assert clock.reads == stamps.calls == len(observed_data) == 3
    assert [
        (d.input_tokens, d.cached_input_tokens, d.cache_creation_tokens)
        for d in observed_data
    ] == [(95, 25, 40), (95, 25, 40), (120, 28, 42)]
    assert [d.output_tokens for d in observed_data] == [30, 30, 35]
    assert [d.reasoning_tokens for d in observed_data] == [10, 10, 12]
    assert [d.observation_sequence for d in observed_data] == [1, 2, 3]
    assert [d.observed_at for d in observed_data] == [
        TIME + SECOND,
        TIME + SECOND,
        TIME + 20 * SECOND,
    ]
    assert [d.coverage.observation_gap for d in observed_data] == [False, True, True]
    for d in observed_data:
        assert (
            d.coverage.observation_id,
            d.coverage.observation_sequence,
            d.coverage.observed_at,
        ) == (d.observation_id, d.observation_sequence, d.observed_at)
        assert d.coverage.attribution_gap
        assert d.coverage.completeness == "partial"
        assert d.model is d.reported_usd is d.cache_creation_by_ttl is None
        assert d.basis == "price_table"
        assert d.billing_class == "api_key"
        assert d.message_id == MESSAGE
        data_json = d.model_dump_json()
        assert THREAD not in data_json
        assert TURN not in data_json
    assert rows == ()
    assert ledger.latest == observed_data[-1]
    assert coverage.completeness == "unavailable"
    assert coverage.attribution_gap
    assert coverage.observation_gap


@pytest.mark.parametrize(
    "phase", ["before-reply", "after-reply", "no-report", "wrong-turn"]
)
@pytest.mark.asyncio
async def test_codex_usage_events_keep_only_confirmed_counts_across_wire_loss(
    phase: Literal["before-reply", "after-reply", "no-report", "wrong-turn"],
) -> None:
    sink = ConformanceSink(None, Trace())
    stamps = Stamps()
    clock = ReceiptClock()
    emitter = CodexUsageEvents(sink, message_id=MESSAGE, next_stamp=stamps.next)
    session = AppServerTurnSession(THREAD, max_messages=2, max_tools=2)
    consumed = asyncio.Event()
    confirmed = asyncio.Event()
    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt="fixture",
                deadline=future_deadline(),
                usage_observer=UsageObserver(clock.now, emitter.observe),
                observe=lambda _notice: consumed.set(),
                on_started=lambda _turn: confirmed.set(),
            )
        )
        await connection.peer_reader.readline()

        if phase in ("after-reply", "no-report"):
            await connection.send(
                started({"turn": {"id": TURN, "status": "inProgress"}})
            )
            await confirmed.wait()
        if phase != "no-report":
            await connection.send(
                usage_frame(FIRST, turn="wrong" if phase == "wrong-turn" else TURN)
            )
            await consumed.wait()
        if phase == "wrong-turn":
            await connection.send(
                started({"turn": {"id": TURN, "status": "inProgress"}})
            )
            with pytest.raises(UsageError) as mismatch:
                await operation
            assert mismatch.value.reason is UsageFailure.IDENTITY_MISMATCH
        else:
            connection.peer_writer.write_eof()
            with pytest.raises(RpcError) as transport:
                await operation
            assert transport.value.reason is RpcFailure.DISCONNECTED

    observed_data = data_from(sink)
    assert stamps.calls == len(observed_data) == int(phase == "after-reply")
    if phase == "after-reply":
        assert observed_data[0].input_tokens == 50
        assert observed_data[0].coverage.completeness == "partial"
        assert session.snapshot().terminal_status is None
    else:
        assert session.snapshot().usage is None


@pytest.mark.parametrize("writes", [None, 0], ids=["absent", "reported-zero"])
def test_codex_usage_events_preserve_known_zero_and_omitted_cache_writes(
    writes: int | None,
) -> None:
    sink = ConformanceSink(None, Trace())
    emitter = CodexUsageEvents(sink, message_id=MESSAGE, next_stamp=Stamps().next)
    observation = ConfirmedUsage(TokenCounts(0, 0, writes, 0, 0), TIME, False)

    emitter.observe(observation)

    (data,) = data_from(sink)
    assert data.input_tokens == data.cache_creation_tokens == 0
    assert ("input_tokens" in data.known_token_fields) == (writes is not None)
    assert ("cache_creation_tokens" in data.known_token_fields) == (writes is not None)
    assert {
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
    } <= data.known_token_fields


def test_codex_usage_events_refuse_overflow_without_advancing_revision() -> None:
    sink = ConformanceSink(None, Trace())
    stamps = Stamps()
    emitter = CodexUsageEvents(sink, message_id=MESSAGE, next_stamp=stamps.next)
    limit = ConfirmedUsage(TokenCounts(1, 0, 0, MAX_TOKEN, 0), TIME, False)
    excess = ConfirmedUsage(TokenCounts(1, 0, 0, MAX_TOKEN + 1, 0), TIME, True)

    emitter.observe(limit)
    with pytest.raises(UsageEventRefused) as caught:
        emitter.observe(excess)

    assert caught.value.reason is UsageEventFailure.INVALID_OBSERVATION
    assert stamps.calls == len(sink.events) == 1
    assert data_from(sink)[0].output_tokens == MAX_TOKEN


@pytest.mark.parametrize(
    ("prior", "next_sequence", "next_id"),
    [
        (MAX_SUM, 0, "new-epoch"),
        (1, 0, "stale"),
        (1, 1, "equal"),
        (1, 2, "initial"),
        (1, MAX_SUM + 1, "over-limit"),
    ],
    ids=["exhaustion", "older", "same-sequence", "same-id", "over-limit"],
)
def test_codex_usage_events_refuse_invalid_revisions_before_emit(
    prior: int, next_sequence: int, next_id: str
) -> None:
    sink = ConformanceSink(None, Trace())
    stamps = iter((UsageStamp("initial", prior), UsageStamp(next_id, next_sequence)))
    emitter = CodexUsageEvents(sink, message_id=MESSAGE, next_stamp=stamps.__next__)
    observation = ConfirmedUsage(FIRST, TIME, False)

    emitter.observe(observation)
    with pytest.raises(UsageEventRefused) as caught:
        emitter.observe(observation)

    assert caught.value.reason is UsageEventFailure.INVALID_REVISION
    assert len(sink.events) == 1
    if prior == MAX_SUM:
        assert next(stamps) == UsageStamp(next_id, next_sequence)


@pytest.mark.asyncio
async def test_codex_usage_events_refuse_native_context_fill_before_emission() -> None:
    sink = ConformanceSink(None, Trace())
    stamps = Stamps()
    emitter = CodexUsageEvents(sink, message_id=MESSAGE, next_stamp=stamps.next)
    session = AppServerTurnSession(THREAD, max_messages=2, max_tools=2)
    synthetic = usage_frame(TokenCounts(0, 0, 0, 0, 0), total_tokens=100_000)
    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt="fixture",
                deadline=future_deadline(),
                usage_observer=UsageObserver(ReceiptClock().now, emitter.observe),
            )
        )
        await connection.peer_reader.readline()

        await connection.send(
            started({"turn": {"id": TURN, "status": "inProgress"}}), synthetic
        )
        with pytest.raises(UsageError) as caught:
            await operation

        assert caught.value.reason is UsageFailure.INVALID_SNAPSHOT
        assert stamps.calls == 0
        assert sink.events == []


def test_codex_usage_events_refuse_unqualified_source_time() -> None:
    sink = ConformanceSink(None, Trace())
    stamps = Stamps()
    emitter = CodexUsageEvents(sink, message_id=MESSAGE, next_stamp=stamps.next)
    without_zone = ConfirmedUsage(FIRST, TIME.replace(tzinfo=None), False)

    with pytest.raises(UsageEventRefused) as caught:
        emitter.observe(without_zone)

    assert caught.value.reason is UsageEventFailure.INVALID_OBSERVATION
    assert stamps.calls == len(sink.events) == 0
