import gc
import time
import tracemalloc
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.plugins.harness.claude.adapter import ClaudeSummary
from kora.plugins.harness.claude.events import TextPiece
from kora.protocols.harness import (
    MAX_SAMPLE_ITEMS,
    MAX_TEXT_BYTES,
    PermissionDeniedData,
    RunnerOutcome,
)
from kora.runner.usage import MAX_CLOSED_MESSAGES, MAX_MODELS, MAX_OPEN_MESSAGES
from tests.measurements.harness import (
    Report,
    Sample,
    current_rss_bytes,
    new_report,
    write_report,
)
from tests.plugins.harness.claude import recording_builder as frames
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    FrozenClock,
    context_for,
    init_fields,
    projector,
)
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_session_resources import (
    Ending,
    open_fds,
    release_cycle,
)
from tests.plugins.harness.claude.test_stream_volume import volume_run


def memory_sample(
    report: Report, workload: str, repeat: int, count: int, started: float
) -> None:
    gc.collect()
    settled, peak = tracemalloc.get_traced_memory()
    component = sum(
        stat.size
        for stat in tracemalloc.take_snapshot()
        .filter_traces(
            (
                tracemalloc.Filter(False, "*/kora/conformance/capture.py"),
                tracemalloc.Filter(False, tracemalloc.__file__),
            )
        )
        .statistics("filename")
    )
    rss = current_rss_bytes()
    report.samples.append(
        Sample(
            workload,
            repeat,
            count,
            time.perf_counter() - started,
            {
                "settled_traced_bytes": settled,
                "settled_without_capture_bytes": component,
                "peak_traced_bytes": peak,
                "rss_bytes": rss,
                "rss_available": rss is not None,
                "fd_count": len(open_fds()),
            },
        )
    )


def finalisation_peak(capture: Capture, report: Report) -> None:
    adapter = make_adapter(Path("/work"), context_for(capture, FrozenClock()))
    _, events = projector()
    events.observe_init(init_fields())
    for index in range(MAX_CLOSED_MESSAGES):
        events.usage_start(
            f"closed-{index}", f"model-{index % MAX_MODELS}", None, {"input_tokens": 1}
        )
        events.usage_delta({"output_tokens": 1})
        events.usage_stop()
    for index in range(MAX_OPEN_MESSAGES):
        events.usage_start(
            f"open-{index}",
            f"model-{index % MAX_MODELS}",
            f"scope-{index}",
            {"input_tokens": 1},
        )
    for index in range(MAX_SAMPLE_ITEMS):
        events.deny(
            PermissionDeniedData(
                tool="Write",
                tool_use_id=f"denied-{index}",
                source="policy",
                code="not_allowed",
            )
        )
    events.assistant("text", None, (TextPiece("x" * (MAX_TEXT_BYTES - 2)),))
    events.flush()
    summary = ClaudeSummary("claude", events)
    tracemalloc.reset_peak()
    before, _ = tracemalloc.get_traced_memory()
    started = time.perf_counter()

    result = adapter.finalise(summary, RunnerOutcome("cancel", "killed", None, 9))

    settled, peak = tracemalloc.get_traced_memory()
    report.samples.append(
        Sample(
            "finalisation",
            0,
            1,
            time.perf_counter() - started,
            {
                "peak_above_start_bytes": peak - before,
                "settled_delta_bytes": settled - before,
                "models": len(result.usage),
                "denial_items": len(result.denials.items),
            },
        )
    )
    assert len(result.usage) == MAX_MODELS
    assert len(result.denials.items) == MAX_SAMPLE_ITEMS


@pytest.mark.measurement
@pytest.mark.asyncio
async def test_claude_session_lifetime(claude_capture: Capture, tmp_path: Path) -> None:
    report = new_report()
    report.environment["method"] = (
        "Serial replay; tracemalloc observer enabled; RSS from proc statm only; "
        "no numerical gates"
    )
    report.environment["relay_method"] = (
        "Ordering diagnostic: nonnegative interval from receiving the pre-result "
        "pause report to the last required journal append; zero means every "
        "record was relayed before the replay paused"
    )
    report.environment["heap_method"] = (
        "settled_without_capture_bytes excludes the log capture fixture and "
        "tracemalloc's own traces"
    )
    endings: tuple[Ending, ...] = ("success", "interrupt", "kill")
    for ending in endings:
        await release_cycle(claude_capture, tmp_path, ending)
    tracemalloc.start()

    try:
        for count in (10_000, 100_000):
            recording = frames.recording(
                tmp_path / f"stream-{count}.ndjson", frames.volume_frames(count)
            )
            gc.collect()
            tracemalloc.reset_peak()
            started = time.perf_counter()
            await volume_run(claude_capture, recording)
            memory_sample(report, "stream", 0, count, started)
        for repeat in range(5):
            tracemalloc.reset_peak()
            started = time.perf_counter()
            for ending in endings:
                await release_cycle(claude_capture, tmp_path, ending)
            memory_sample(report, "release", repeat, len(endings), started)
        recording = frames.recording(
            tmp_path / "relay.ndjson", frames.volume_frames(10, pause=True)
        )
        _, latency = await volume_run(claude_capture, recording, pause=True)
        assert latency is not None
        report.environment["relay_after_pause_ns"] = latency
        finalisation_peak(claude_capture, report)
    finally:
        tracemalloc.stop()
        write_report(report)

    assert {sample.workload for sample in report.samples} == {
        "stream",
        "release",
        "finalisation",
    }
    assert len(report.samples) == 8
