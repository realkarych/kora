import asyncio
import threading
import time
from collections import Counter
from pathlib import Path
from tempfile import mkdtemp

import pytest

from kora.conformance.capture import Capture
from kora.conformance.harness import ConformancePolicy, ConformanceSink, Trace
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability.redaction import MASK, SecretRegistry
from kora.plugins.harness.claude.events import ClaudeEvents, TextPiece, TurnText
from kora.plugins.harness.text import (
    MARKER,
    MASK_CHUNK_BYTES,
    encoded_text_size,
    masked_text,
)
from kora.protocols.harness import MAX_TEXT_BYTES, TurnEvent
from kora.protocols.time import Clock
from kora.runner.journal import JournalAppendOutcome
from kora.runner.sink import RunnerSink
from tests.plugins.harness.claude import recording_builder as frames
from tests.plugins.harness.claude.conformance_launch import ClaudeLaunches, wait_signal
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    WHEN,
    FrozenClock,
    context_for,
    init_fields,
)
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_session_lifecycle import session_spec


class MemoryJournal:
    def __init__(self, *, blocked: bool = False) -> None:
        self.records: list[tuple[str, bytes]] = []
        self.appended_at: list[int] = []
        self.loop = asyncio.get_running_loop()
        self.changed = asyncio.Event()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome:
        self.loop.call_soon_threadsafe(self.entered.set)
        self.release.wait()
        self.records.append((kind, canonical_bytes(data)))
        self.appended_at.append(time.monotonic_ns())
        self.loop.call_soon_threadsafe(self.changed.set)
        return JournalAppendOutcome.DURABLE

    async def wait_for_kinds(self, kinds: set[str]) -> None:
        async with asyncio.timeout(10):
            while not kinds.issubset(kind for kind, _ in self.records):
                self.changed.clear()
                await self.changed.wait()


async def close_sink(sink: RunnerSink, clock: Clock) -> None:
    try:
        assert await sink.settle(clock.monotonic_ns() + 5_000_000_000)
    finally:
        sink.close()
        assert await sink.joined(clock.monotonic_ns() + 5_000_000_000)
    assert sink.omitted() == 0


async def volume_run(
    capture: Capture, recording: Path, *, pause: bool = False
) -> tuple[MemoryJournal, int | None]:
    launches = ClaudeLaunches(Path(mkdtemp(prefix="kc", dir="/tmp")), recording)
    clock = FrozenClock()
    adapter = make_adapter(
        launches.workspace, context_for(capture, clock), max_retries=2
    )
    journal = MemoryJournal()
    sink = RunnerSink(journal, adapter.registry, clock, None, 900_000_000_000)
    policy = ConformancePolicy("Forbidden", Trace())
    task = asyncio.create_task(
        adapter.run(session_spec(adapter), policy, sink, launches)
    )
    relay_ns: int | None = None
    try:
        async with asyncio.timeout(30):
            if pause:
                await wait_signal(launches.allocated, task)
                assert launches.launch is not None
                await wait_signal(launches.launch.paused, task)
                paused_at = time.monotonic_ns()
                await journal.wait_for_kinds(
                    {"turn", "tool_call", "tool_result", "usage"}
                )
                relay_ns = max(0, journal.appended_at[-1] - paused_at)
                assert not task.done()
                launches.release_pause()
            result = await task
        assert result.stop_reason == "completed"
        assert result.error_class is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await launches.close()
        await close_sink(sink, clock)
    return journal, relay_ns


@pytest.mark.asyncio
async def test_claude_stream_volume_is_bounded(
    claude_capture: Capture, tmp_path: Path
) -> None:
    recordings = [
        frames.recording(
            tmp_path / f"volume-{count}.ndjson", frames.volume_frames(count)
        )
        for count in (10_000, 100_000)
    ]

    journals = [
        (await volume_run(claude_capture, recording))[0] for recording in recordings
    ]

    assert [kind for kind, _ in journals[0].records] == [
        kind for kind, _ in journals[1].records
    ]
    assert sum(len(data) for _, data in journals[0].records) == sum(
        len(data) for _, data in journals[1].records
    )
    for journal in journals:
        kinds = Counter(kind for kind, _ in journal.records)
        assert kinds == {
            "session_started": 1,
            "turn": 3,
            "tool_call": 1,
            "tool_result": 1,
            "usage": 2,
            "result": 1,
            "provider_retrying": 2,
        }


@pytest.mark.asyncio
async def test_claude_events_relayed_before_result(
    claude_capture: Capture, tmp_path: Path
) -> None:
    recording = frames.recording(
        tmp_path / "paused.ndjson", frames.volume_frames(10, pause=True)
    )

    journal, latency = await volume_run(claude_capture, recording, pause=True)

    assert latency is not None
    assert journal.records[-1][0] == "result"


@pytest.mark.asyncio
async def test_stuck_journal_delays_neither_callbacks_nor_interrupts(
    claude_capture: Capture, tmp_path: Path
) -> None:
    recording = frames.recording(
        tmp_path / "stuck.ndjson",
        [
            frames.init_frame(),
            frames.hook("Write", "one"),
            {"kora_replay": "await_interrupt"},
            frames.success(),
        ],
    )
    launches = ClaudeLaunches(Path(mkdtemp(prefix="kc", dir="/tmp")), recording)
    clock = FrozenClock()
    adapter = make_adapter(launches.workspace, context_for(claude_capture, clock))
    journal = MemoryJournal(blocked=True)
    sink = RunnerSink(journal, adapter.registry, clock, None, 900_000_000_000)
    task = asyncio.create_task(
        adapter.run(
            session_spec(adapter),
            ConformancePolicy("Forbidden", Trace()),
            sink,
            launches,
        )
    )

    try:
        async with asyncio.timeout(10):
            await wait_signal(journal.entered, task)
            assert launches.launch is not None
            await launches.launch.decided.wait()
            assert '"allow"' in launches.recording.with_suffix(".decisions").read_text()
            sink.terminate("cancel")
            await launches.launch.interrupted.wait()
            assert not journal.release.is_set()
            assert journal.records == []
            journal.release.set()
            result = await task
        assert result.stop_reason == "cancel"
    finally:
        journal.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await launches.close()
        await close_sink(sink, clock)


def text_projector(registry: SecretRegistry) -> tuple[ConformanceSink, ClaudeEvents]:
    sink = ConformanceSink(None, Trace())
    events = ClaudeEvents(
        sink,
        registry,
        binding="claude-test",
        max_retries=2,
        output_format=False,
        check_deadline=lambda: None,
        observed_at=lambda: WHEN,
    )
    events.observe_init(init_fields())
    return sink, events


@pytest.mark.parametrize("split", [1, 8, 8191])
def test_turn_text_masks_secret_split_across_blocks(split: int) -> None:
    registry = SecretRegistry()
    registry.register("secret-across-blocks")
    text = "a" * (split - 1) + 'secret-across-blocks\n"é😀'
    sink, events = text_projector(registry)
    expected = masked_text(text, registry, lambda: None)

    events.assistant("one", None, (TextPiece(text[:split]),))
    events.assistant("one", None, (TextPiece(text[split:]),))
    events.flush()

    turn = sink.events[-1]
    assert isinstance(turn, TurnEvent)
    assert (turn.data.text, turn.data.truncated) == expected
    assert turn.data.text is not None
    assert "secret-across-blocks" not in turn.data.text
    assert MASK in turn.data.text


@pytest.mark.parametrize("piece", ["x", '\n"é😀'])
def test_turn_text_over_budget_is_truncated(piece: str) -> None:
    sink, events = text_projector(SecretRegistry())
    text = piece * (MAX_TEXT_BYTES // 2)

    for _ in range(4):
        events.assistant("one", None, (TextPiece(text),))
    events.flush()

    turn = sink.events[-1]
    assert isinstance(turn, TurnEvent)
    assert turn.data.truncated
    assert turn.data.text is not None
    assert encoded_text_size(turn.data.text) <= MAX_TEXT_BYTES


def test_turn_text_retained_state_is_bounded_before_flush() -> None:
    registry = SecretRegistry()
    registry.register("secret-across-blocks")
    text = TurnText(registry)
    chunk = "x" * MASK_CHUNK_BYTES
    bound = MAX_TEXT_BYTES + MASK_CHUNK_BYTES

    for _ in range(1_000):
        text.feed(chunk, lambda: None)
        assert sum(encoded_text_size(part) - 2 for part in text.parts) + 2 <= bound
        assert text.masker.held <= len("secret-across-blocks")
        assert len(text.pending) < MASK_CHUNK_BYTES
    rendered, truncated = text.finish()

    assert truncated
    assert rendered is not None
    assert encoded_text_size(rendered) <= MAX_TEXT_BYTES


@pytest.mark.parametrize("size", [MAX_TEXT_BYTES - 2, MAX_TEXT_BYTES - 1])
def test_turn_text_canonical_boundary(size: int) -> None:
    registry = SecretRegistry()
    text = TurnText(registry)
    original = "x" * size

    for start in range(0, size, 17):
        text.feed(original[start : start + 17], lambda: None)
    result = text.finish()

    assert result == masked_text(original, registry, lambda: None)


def test_turn_text_preserves_overlapping_secret_masking() -> None:
    registry = SecretRegistry()
    registry.register("abababab")
    sink, events = text_projector(registry)
    original = "ababababab"

    events.assistant("one", None, (TextPiece(original[:8]),))
    events.assistant("one", None, (TextPiece(original[8:]),))
    events.flush()

    turn = sink.events[-1]
    assert isinstance(turn, TurnEvent)
    assert (turn.data.text, turn.data.truncated) == masked_text(
        original, registry, lambda: None
    )


def test_turn_text_masks_secret_across_truncation_cut() -> None:
    registry = SecretRegistry()
    registry.register("secret-at-the-cut")
    sink, events = text_projector(registry)
    prefix = "a" * (MAX_TEXT_BYTES - encoded_text_size(MARKER) - len("secret-at"))
    text = prefix + "secret-at-the-cut" + "b" * 64

    events.assistant("one", None, (TextPiece(text[: len(prefix) + 4]),))
    events.assistant("one", None, (TextPiece(text[len(prefix) + 4 :]),))
    events.flush()

    turn = sink.events[-1]
    assert isinstance(turn, TurnEvent)
    assert turn.data.truncated
    assert turn.data.text is not None
    assert "secret" not in turn.data.text
    assert (turn.data.text, turn.data.truncated) == masked_text(
        text, registry, lambda: None
    )
