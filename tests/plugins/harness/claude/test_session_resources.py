import asyncio
import errno
import gc
import os
import signal
import threading
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal

import pytest
from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk._internal.query import Query

from kora.conformance.capture import Capture
from kora.conformance.harness import ConformancePolicy, ConformanceSink, Trace
from kora.plugins.harness.claude.run import INTERRUPT_GRACE_NS, ClaudeSession
from kora.protocols.harness import SessionResult
from tests.plugins.harness.claude import recording_builder as frames
from tests.plugins.harness.claude.conformance_launch import (
    RECORDINGS,
    ClaudeLaunches,
    wait_signal,
)
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_replay_events import context_for
from tests.plugins.harness.claude.test_session_lifecycle import (
    CleaningPolicy,
    ObservedHarness,
    TeardownClock,
    oversized_recording,
    session_spec,
)

Ending = Literal[
    "success",
    "interrupt",
    "kill",
    "launcher-death",
    "oversized",
    "cancellation",
    "initialize-deadline",
    "internal-escalation",
    "post-result-tail",
]
ENDINGS: tuple[Ending, ...] = (
    "success",
    "interrupt",
    "kill",
    "launcher-death",
    "oversized",
    "cancellation",
    "initialize-deadline",
    "internal-escalation",
    "post-result-tail",
)
PENDING_LEGS: dict[str, str] = {
    "upload-and-transcript-sizes": "#199",
    "session-file-symlink-or-fifo": "#184",
    "detached-target-child": "#184",
    "mcp-processes": "#184",
    "runner-shutdown": "#199",
    "unanswered-mcp-status": (
        "#1388 MCP server support refused by the first-slice configuration"
    ),
}
VOLUME_LEGS = {
    "provider-retrying-projection",
    "journal-volume",
    "relay-before-result",
    "stuck-journal",
    "split-secret",
    "bounded-turn-text",
    "event-counting",
}


def open_fds() -> set[int]:
    result: set[int] = set()
    for name in os.listdir("/dev/fd"):
        descriptor = int(name)
        try:
            os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue
        result.add(descriptor)
    return result


def ending_recording(directory: Path, ending: Ending) -> Path:
    if ending == "oversized":
        return oversized_recording(directory)
    if ending == "launcher-death":
        return frames.recording(
            directory / "death.ndjson",
            [frames.init_frame(), {"kora_replay": "await_interrupt"}],
        )
    if ending == "internal-escalation":
        source = (RECORDINGS / "conformance-bypass-2.1.301.ndjson").read_text()
        recording = directory / "ignored-bypass.ndjson"
        recording.write_text(source.replace('"await_interrupt"', '"ignore_interrupt"'))
        return recording
    if ending == "post-result-tail":
        return frames.recording(
            directory / "tail.ndjson",
            [
                frames.init_frame(),
                frames.message_start("main"),
                frames.message_delta(),
                frames.message_stop(),
                frames.assistant("main", frames.text_block("done")),
                frames.success(),
                {"kora_replay": "linger"},
                frames.message_start("tail"),
                frames.message_delta(),
                frames.message_stop(),
            ],
        )
    if ending == "success":
        return RECORDINGS / "constructed-retry-capped-2.1.301.ndjson"
    name = {
        "interrupt": "interrupt",
        "kill": "ignore_interrupt",
        "cancellation": "tool_calls",
        "initialize-deadline": "no_usage",
    }[ending]
    return RECORDINGS / f"conformance-{name}-2.1.301.ndjson"


async def release_cycle(capture: Capture, directory: Path, ending: Ending) -> int:
    clock = TeardownClock()
    launches = ClaudeLaunches(
        Path(mkdtemp(prefix="kc", dir="/tmp")),
        ending_recording(directory, ending),
        "ignore_initialize" if ending == "initialize-deadline" else "2.1.301",
    )
    base = make_adapter(launches.workspace, context_for(capture, clock), max_retries=2)
    adapter = ObservedHarness(base.scope, base.ctx, base.registry, base.environment)
    sink = ConformanceSink(None, Trace())
    policy = (
        CleaningPolicy()
        if ending == "cancellation"
        else ConformancePolicy("Forbidden", sink.trace)
    )
    task = asyncio.create_task(
        adapter.run(session_spec(adapter), policy, sink, launches)
    )
    result: SessionResult | None = None
    try:
        async with asyncio.timeout(10):
            await wait_signal(launches.allocated, task)
            assert launches.launch is not None
            launch = launches.launch
            await launch.ready.wait()
            assert launch.pid is not None
            pid = launch.pid
            if ending in {"interrupt", "kill"}:
                await wait_signal(launch.waiting, task)
                sink.stop("cancel")
                await launch.interrupted.wait()
                if ending == "kill":
                    adapter.kill()
            elif ending == "launcher-death":
                await wait_signal(launch.waiting, task)
                os.killpg(pid, signal.SIGKILL)
            elif ending == "cancellation":
                assert isinstance(policy, CleaningPolicy)
                await policy.entered.wait()
                task.cancel()
                await policy.cleaning.wait()
                policy.release.set()
            elif ending == "initialize-deadline":
                await launch.initializing.wait()
                sink.stop("deadline")
                await launch.interrupted.wait()
                deadline = clock.monotonic_ns() + 30_000_000_000
                escalation = asyncio.create_task(
                    host_escalation(adapter, clock, deadline)
                )
                try:
                    await clock.waiting.wait()
                    assert clock.deadline == deadline
                    clock.advance()
                    await escalation
                finally:
                    escalation.cancel()
                    await asyncio.gather(escalation, return_exceptions=True)
            elif ending == "internal-escalation":
                started = clock.monotonic_ns()
                await launch.interrupted.wait()
                await clock.waiting.wait()
                assert clock.deadline == started + INTERRUPT_GRACE_NS
                clock.advance()
            if ending == "cancellation":
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert isinstance(policy, CleaningPolicy)
                assert policy.closed
            else:
                result = await task
        if result is not None:
            expected = {
                "success": "completed",
                "interrupt": "cancel",
                "kill": "cancel",
                "launcher-death": "launcher_died",
                "oversized": "oversized_message",
                "initialize-deadline": "deadline",
                "internal-escalation": "policy_bypass",
                "post-result-tail": "completed",
            }
            assert result.stop_reason == expected[ending]
            if ending == "success":
                assert (
                    sum(event.kind == "provider_retrying" for event in sink.events) == 2
                )
            assert adapter.outcome is not None
            if ending in {"initialize-deadline", "internal-escalation"}:
                assert adapter.outcome.termination == "killed_after_grace"
            if ending == "post-result-tail":
                assert adapter.outcome.termination == "teardown_after_result"
                assert result.usage_coverage.completeness == "complete"
        return pid
    finally:
        if isinstance(policy, CleaningPolicy):
            policy.release.set()
        if not task.done():
            adapter.kill()
            if launches.launch is not None:
                await launches.launch.kill()
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await launches.close()


async def host_escalation(
    adapter: ObservedHarness, clock: TeardownClock, deadline: int
) -> None:
    await clock.sleep_until(deadline)
    adapter.kill()


@pytest.mark.asyncio
async def test_claude_session_releases_resources(
    claude_capture: Capture, tmp_path: Path
) -> None:
    tasks = asyncio.all_tasks()
    threads = set(threading.enumerate())
    descriptors = open_fds()
    pids: list[int] = []

    for _ in range(2):
        for ending in ENDINGS:
            pids.append(await release_cycle(claude_capture, tmp_path, ending))
    gc.collect()

    assert asyncio.all_tasks() == tasks
    assert set(threading.enumerate()) == threads
    assert open_fds() == descriptors
    assert not any(
        isinstance(value, (ClaudeSDKClient, Query, ClaudeSession))
        for value in gc.get_objects()
    )
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)


def test_resource_and_volume_legs_have_owners() -> None:
    executed = (
        set(ENDINGS) | VOLUME_LEGS | {"per-attempt-directories", "launcher-processes"}
    )
    expected = {
        "success",
        "interrupt",
        "kill",
        "launcher-death",
        "oversized",
        "cancellation",
        "initialize-deadline",
        "internal-escalation",
        "post-result-tail",
        "journal-volume",
        "relay-before-result",
        "stuck-journal",
        "split-secret",
        "bounded-turn-text",
        "event-counting",
        "upload-and-transcript-sizes",
        "session-file-symlink-or-fifo",
        "per-attempt-directories",
        "detached-target-child",
        "launcher-processes",
        "mcp-processes",
        "runner-shutdown",
        "unanswered-mcp-status",
        "provider-retrying-projection",
    }

    owned = executed | PENDING_LEGS.keys()

    assert owned == expected
    assert executed.isdisjoint(PENDING_LEGS)
    assert all(owner.startswith("#") for owner in PENDING_LEGS.values())
