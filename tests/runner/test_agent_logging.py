import asyncio
import json
import os
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture, Pipe
from kora.observability import LoggingSettings, configure_process
from kora.observability.context import bound_scope, current_context
from kora.protocols.runtime import SessionResultObservation
from kora.protocols.secret_value import SecretScope
from kora.runner.agent import Released
from tests.plugins.harness.fake.fake_support import result, tool_call, turn
from tests.runner.agent_support import (
    FIXED_NOW,
    GatedScript,
    JournalProxy,
    execution,
    hosted,
    journal_threads_exited,
    make_world,
    run,
    script,
)
from tests.support.local_peers import until

MARKER: Final[str] = "kora-unregistered-agent-content-4d1c"
LEAKED_VALUE: Final[str] = "kora-registered-agent-value-9e2f"


CONTENT: Final[str] = f"{MARKER} {LEAKED_VALUE}"


def test_logging_redacts_content_and_safe_exception_chains(
    tmp_path: Path, kora_capture: Capture
) -> None:
    kora_capture.scope.admit(LEAKED_VALUE)
    worlds = [make_world(tmp_path / name, kora_capture) for name in ("a", "b", "c")]
    shell = tool_call("Bash", "call-1", arguments={"command": CONTENT})
    closing = JournalProxy(worlds[2].journal, "turn", "close")

    run(hosted(worlds[0], script([turn("m1", CONTENT), shell, result()])))
    run(hosted(worlds[1], script([turn("m1", CONTENT)])))
    run(hosted(worlds[2], script([turn("m1", CONTENT), result()]), closing))
    for world in worlds:
        world.close()
    records = kora_capture.finish()

    agent = [record for record in records if record.get("component") == "runner.agent"]
    events = [record.get("event") for record in agent]
    failures = [
        record.get("code")
        for record in agent
        if record.get("event") == "runner.agent.session_failed"
    ]
    text = "\n".join(json.dumps(record) for record in records)
    assert events.count("runner.agent.session_started") == 3
    assert events.count("runner.agent.session_finished") == 1
    assert "runner.agent.journal_gap" in events
    assert failures == ["script_end_invalid", "result_unsealed"]
    assert MARKER not in text
    assert LEAKED_VALUE not in text
    assert str(tmp_path) not in text


def test_candidate_output_cannot_forge_component_diagnostics(
    tmp_path: Path, kora_capture: Capture
) -> None:
    forged = (
        json.dumps(
            {
                "component": "runner.agent",
                "event": "runner.agent.session_finished",
                "attempt_id": MARKER,
            }
        )
        + "\n\r\x1b[31m"
    )
    world = make_world(tmp_path, kora_capture)
    call = tool_call("Bash", "call-1")
    call["outcome"] = {"is_error": False, "content": forged}
    source = script(
        [
            turn("m1", forged),
            call,
            {
                "kind": "event",
                "event": "error",
                "data": {"code": "harness_failed", "text": forged, "truncated": False},
            },
            result(),
        ]
    )

    try:
        host, released = run(hosted(world, source))
        observation = host.observation()
    finally:
        world.close()
    records = kora_capture.finish()

    events = [record.get("event") for record in records]
    assert events.count("runner.agent.session_started") == 1
    assert events.count("runner.agent.session_finished") == 1
    assert MARKER not in json.dumps(records)
    assert isinstance(observation, SessionResultObservation)
    assert released == Released(session=True, writer=True)
    contents: list[str] = []
    for record in world.records():
        data = record["data"]
        assert isinstance(data, dict)
        for field in ("text", "content"):
            value = data.pop(field, None)
            if isinstance(value, str):
                contents.append(value)
        assert MARKER not in json.dumps(record)
    assert contents == [forged, forged, forged]
    assert journal_threads_exited()


def test_logging_context_isolated_and_propagated(
    tmp_path: Path, kora_capture: Capture
) -> None:
    worlds = [make_world(tmp_path / name, kora_capture) for name in ("one", "two")]
    sources = [GatedScript(script([turn("m1"), result()])) for _ in worlds]
    hosts = [
        world.host(world.resolver(source))
        for world, source in zip(worlds, sources, strict=True)
    ]

    async def both() -> None:
        for index, (world, host) in enumerate(zip(worlds, hosts, strict=True)):
            with bound_scope(attempt_id=f"attempt-{index + 1}"):
                host.start(world.workload(), world.workspace())
        await asyncio.gather(*(source.entered.wait() for source in sources))
        for source in sources:
            source.release.set()
        await asyncio.gather(*(host.finished() for host in hosts))
        assert current_context() == {}
        for host in hosts:
            assert await host.stop() == Released(session=True, writer=True)

    try:
        run(both())
    finally:
        for source in sources:
            source.release.set()
        for world in worlds:
            world.close()
    records = kora_capture.finish()

    for component in ("runner.agent", "harness.fake"):
        selected = [
            record
            for record in records
            if str(record.get("event")).startswith(component + ".")
        ]
        assert selected
        assert {record.get("attempt_id") for record in selected} == {
            "attempt-1",
            "attempt-2",
        }
        for attempt_id in ("attempt-1", "attempt-2"):
            events = [
                record.get("event")
                for record in selected
                if record.get("attempt_id") == attempt_id
            ]
            if component == "runner.agent":
                assert events.count(f"{component}.session_started") == 1
            assert events.count(f"{component}.session_finished") == 1
    assert current_context() == {}
    assert journal_threads_exited()


@pytest.mark.parametrize("blocked", [True, False], ids=["blocked", "broken"])
def test_logging_sink_failure_does_not_stall_work(
    tmp_path: Path, blocked: bool
) -> None:
    pipe = Pipe.open()
    if blocked:
        os.set_blocking(pipe.write_end, False)
        try:
            while True:
                os.write(pipe.write_end, b"x" * 4096)
        except BlockingIOError:
            os.set_blocking(pipe.write_end, True)
    else:
        pipe.close_read_end()
    diagnostics = configure_process(
        "runner.agent",
        LoggingSettings(queue_records=16),
        "agent-sink",
        lambda: FIXED_NOW,
        pipe.write_end,
    )
    capture = Capture(diagnostics, SecretScope(diagnostics), pipe, 10)
    world = make_world(tmp_path, capture)
    source = script(
        [
            turn("m1"),
            *[tool_call("Bash", f"call-{index}") for index in range(64)],
            result(),
        ]
    )

    try:
        host, released = run(
            hosted(world, source, execution_request=execution(deny=("Bash(*)",)))
        )
        observation = host.observation()
        if not blocked:
            run(until(lambda: diagnostics.snapshot().sink_failed))
        state = diagnostics.snapshot()
    finally:
        world.close()
        pipe.close_read_end()
        capture.scope.release()
        diagnostics.shutdown(10)
        pipe.close_write_end()

    assert isinstance(observation, SessionResultObservation)
    assert host.result_reference() is not None
    assert released == Released(session=True, writer=True)
    if blocked:
        assert state.dropped_total() > 0
        assert state.queue_records <= 16
    else:
        assert state.sink_failed
    assert not diagnostics.writer_is_alive()
    assert journal_threads_exited()
