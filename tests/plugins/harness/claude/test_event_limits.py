import asyncio
import json
from pathlib import Path
from tempfile import mkdtemp

import pytest

from kora.conformance.capture import Capture
from kora.conformance.harness import ConformancePolicy, ConformanceSink, Trace
from kora.ir import ErrorClass
from kora.ir.config import NodeLimits
from kora.ir.json import JsonObject
from kora.protocols.harness import ToolCallEvent, TurnEvent
from kora.runner.calls import LIMITED_KINDS
from tests.plugins.harness.claude import recording_builder as frames
from tests.plugins.harness.claude.conformance_launch import ClaudeLaunches
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import FrozenClock, context_for
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_session_lifecycle import session_spec


def counting_frames(limited: bool) -> list[JsonObject]:
    opening = [
        frames.init_frame(),
        frames.injected_text(blocks=False),
        frames.injected_text(blocks=True),
        frames.assistant(
            "synthetic", frames.text_block("ignored"), model="<synthetic>"
        ),
        frames.hook("Write", "one"),
        frames.assistant("first", frames.text_block("hello")),
        frames.assistant("first", frames.text_block(" world")),
        frames.assistant("first", frames.tool_block("Write", "one")),
        frames.tool_result("one"),
    ]
    if limited:
        return [
            *opening,
            {"kora_replay": "await_interrupt"},
            frames.hook("StructuredOutput", "output"),
            frames.success(),
        ]
    return [
        *opening,
        frames.hook("StructuredOutput", "output"),
        frames.assistant("second", frames.tool_block("StructuredOutput", "output")),
        frames.tool_result("output"),
        frames.success(),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_events", [None, 3])
async def test_claude_max_events_counted_by_kora(
    claude_capture: Capture,
    tmp_path: Path,
    max_events: int | None,
) -> None:
    recording = frames.recording(
        tmp_path / "count.ndjson", counting_frames(max_events is not None)
    )
    launches = ClaudeLaunches(Path(mkdtemp(prefix="kc", dir="/tmp")), recording)
    sink = ConformanceSink(max_events, Trace())
    adapter = make_adapter(
        launches.workspace, context_for(claude_capture, FrozenClock()), True
    )
    spec = session_spec(
        adapter,
        NodeLimits() if max_events is None else NodeLimits(max_events=max_events),
        {"output_schema": {"type": "object"}},
    )
    policy = ConformancePolicy("Forbidden", sink.trace)

    try:
        async with asyncio.timeout(10):
            result = await adapter.run(spec, policy, sink, launches)
        decisions = [
            json.loads(line)
            for line in launches.recording.with_suffix(".decisions")
            .read_text()
            .splitlines()
        ]
    finally:
        await launches.close()

    turns = [event for event in sink.events if isinstance(event, TurnEvent)]
    calls = [event for event in sink.events if isinstance(event, ToolCallEvent)]
    assert result.turns == len(turns) == (1 if max_events else 2)
    assert result.turns != frames.NATIVE_TURN_COUNT
    assert turns[0].data.text == "hello world"
    assert [event.data.tool_use_id for event in calls] == (
        ["one"] if max_events else ["one", "output"]
    )
    assert sum(event.kind == "tool_result" for event in sink.events) == (
        1 if max_events else 2
    )
    assert not any(event.kind == "permission_denied" for event in sink.events)
    assert decisions[-1]["hookSpecificOutput"]["permissionDecision"] == (
        "deny" if max_events else "allow"
    )
    if max_events:
        assert sum(event.kind in LIMITED_KINDS for event in sink.events) == max_events
        assert result.stop_reason == "max_events"
        assert result.error_class is ErrorClass.EVENTS_EXHAUSTED
    else:
        assert calls[-1].data.output_channel
        assert result.stop_reason == "completed"
        assert result.error_class is None
