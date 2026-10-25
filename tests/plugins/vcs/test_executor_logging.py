import asyncio
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, Pipe
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.conformance.vcs import Prepared, published
from kora.executor.captures import Captures
from kora.executor.failures import ExecutorError
from kora.executor.steps import run_step
from kora.observability import LoggingSettings, configure_process
from kora.protocols.common import ProtocolContext
from kora.protocols.secret_value import SecretScope
from tests.executor.step_support import context, step
from tests.plugins.vcs.git_support import World, git, make_world, write

MARKER: str = "kora-unregistered-content-marker-5e2a"
LEAKED_VALUE: str = "kora-registered-value-8b3c"
FORGED: str = json.dumps(
    {"event": "executor.phase.finished", "component": "executor", "phase": MARKER}
)


def text_of(records: list[dict[str, object]]) -> str:
    return "\n".join(json.dumps(record) for record in records)


def prepared_in(world: World, name: str) -> Prepared:
    checkout = world.checkout()
    write(world.candidate(checkout), {f"{name}.txt": f"{name}\n"})
    return world.prepare(checkout)


def test_logging_context_isolated_and_propagated(
    tmp_path: Path, kora_capture: Capture
) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = make_world(tmp_path / "one", kora_capture)
    second = make_world(tmp_path / "two", kora_capture)
    one, two = prepared_in(first, "one"), prepared_in(second, "two")

    async def both() -> None:
        await asyncio.gather(
            published(first.executor, one, bound_context(first, "attempt-one")),
            published(second.executor, two, bound_context(second, "attempt-two")),
        )

    asyncio.run(both())
    records = kora_capture.finish()

    pushed = [
        record
        for record in records
        if record.get("event") == "executor.phase.finished"
        and record.get("phase") == "publish"
    ]
    assert sorted(str(record.get("attempt_id")) for record in pushed) == [
        "attempt-one",
        "attempt-two",
    ]


def bound_context(world: World, attempt: str) -> ProtocolContext:
    base = world.context()
    return replace(base, logger=base.logger.bind(attempt_id=attempt))


def test_logging_redacts_content_and_safe_exception_chains(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    world.commit(checkout, {f"{MARKER}.txt": f"{LEAKED_VALUE}\n"}, MARKER)
    secret = kora_capture.scope.admit(LEAKED_VALUE)
    subject = context(kora_capture, tmp_path / "steps")
    failing = step(
        f"import sys; print({MARKER!r}); sys.stderr.write({MARKER!r}); sys.exit(2)",
        arguments=(MARKER,),
    )

    with pytest.raises(ExecutorError):
        world.prepare(checkout, secrets=(secret,))
    with pytest.raises(ExecutorError):
        asyncio.run(run_step(failing, 0, subject, Captures()))
    records = kora_capture.finish()

    text = text_of(records)
    events = {record.get("event") for record in records}
    assert {"executor.content.refused", "executor.step.failed"} <= events
    assert MARKER not in text
    assert LEAKED_VALUE not in text
    assert str(tmp_path) not in text


def test_candidate_output_cannot_forge_component_diagnostics(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    forger = tmp_path / "forger.sh"
    forger.write_text(f"#!/bin/sh\necho '{FORGED}'\necho '{FORGED}' >&2\n")
    forger.chmod(0o755)
    git(world.home, "config", "core.fsmonitor", str(forger), cwd=candidate)
    write(candidate, {"forged.txt": "forged\n"})

    prepared = world.prepare(checkout)
    world.publish(prepared)
    records = kora_capture.finish()

    assert MARKER not in text_of(records)
    assert all(record.get("phase") != MARKER for record in records)


def test_logging_multiprocess_framing_with_slow_reader(
    tmp_path: Path, kora_capture: Capture
) -> None:
    subject = context(kora_capture, tmp_path)
    noisy = step(
        "import sys\n"
        f"sys.stdout.write({MARKER!r} * 200000)\n"
        f"sys.stderr.write({MARKER!r} * 200000)\n"
    )
    started = time.monotonic()

    _, outcome = asyncio.run(run_step(noisy, 0, subject, Captures()))
    elapsed = time.monotonic() - started
    records = kora_capture.finish()

    assert outcome.succeeded
    assert outcome.stderr_dropped > 0
    assert elapsed < 10
    assert MARKER not in text_of(records)


def test_logging_sink_failure_does_not_stall_work(tmp_path: Path) -> None:
    pipe = Pipe.open()
    pipe.close_read_end()
    diagnostics = configure_process(
        CONFORMANCE_COMPONENT,
        LoggingSettings(),
        "broken-sink",
        lambda: datetime.now(UTC),
        pipe.write_end,
    )
    capture = Capture(diagnostics, SecretScope(diagnostics), pipe, 5.0)
    try:
        world = make_world(tmp_path, capture)
        prepared = prepared_in(world, "sink")
        started = time.monotonic()
        outcome = world.publish(prepared)
        elapsed = time.monotonic() - started
    finally:
        capture.scope.release()
        diagnostics.shutdown(5.0)
        pipe.close_write_end()

    assert outcome.state == "pushed"
    assert elapsed < 30
    assert os.path.isdir(prepared.result.revision.location)
