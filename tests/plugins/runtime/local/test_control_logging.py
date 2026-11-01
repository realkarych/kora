import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, Pipe, captured
from kora.observability import Level, LoggingSettings, configure_process
from kora.plugins.runtime.local.channel import FrameType
from kora.plugins.runtime.local.serving import COMPONENT, REVERIFY_NS
from kora.protocols.secret_value import SecretScope
from kora.runner.identity import NativeIdentityReader
from kora.runner.peers import kernel_source
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_peers import (
    CONDITION_SECONDS,
    FAR_NS,
    asked_status,
    client,
    served,
    until,
)
from tests.support.local_runners import short_directory
from tests.support.local_runtime import open_descriptors

MARKER = "unregistered-local-control-content-7b8e"


async def malformed_request(path: str, clock: StepClock) -> None:
    channel = await client(path, clock)
    try:
        await channel.write_typed_frame(
            FrameType.JSON,
            f'{{"private":"{MARKER}"}}'.encode(),
            clock.monotonic_ns() + FAR_NS,
        )
        await channel.expect_eof(clock.monotonic_ns() + FAR_NS)
    finally:
        channel.close()


@pytest.mark.asyncio
async def test_logging_redacts_content_and_safe_exception_chains(
    tmp_path: Path,
) -> None:
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    with (
        captured(
            COMPONENT, LoggingSettings(level=Level.DEBUG), "control-logging", clock.now
        ) as capture,
        short_directory() as base,
    ):
        async with served(base / "sockets", clock, source, capture) as (server, path):
            await malformed_request(path, clock)
            await malformed_request(path, clock)
            await until(lambda: server.connections == 0)

            clock.advance(REVERIFY_NS)
            await until(lambda: bool(capture.reader.records()))
            await malformed_request(path, clock)
        records = capture.finish()

    refusals = [
        record
        for record in records
        if record.get("event") == "runtime.local.runner.auth_refused"
    ]
    assert [(record.get("reason"), record.get("count")) for record in refusals] == [
        ("malformed", 2),
        ("malformed", 1),
    ]
    assert all(record.get("level") == "WARNING" for record in refusals)
    assert MARKER not in str(records)
    assert str(base) not in str(records)
    assert all(
        not {"pid", "path", "identity", "exception"} & record.keys()
        for record in refusals
    )


@pytest.mark.asyncio
async def test_logging_sink_failure_does_not_stall_work(tmp_path: Path) -> None:
    descriptors = open_descriptors()
    tasks = set(asyncio.all_tasks())
    pipe = Pipe.open()
    pipe.close_read_end()
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "broken-control-sink",
        lambda: datetime.now(UTC),
        pipe.write_end,
    )
    capture = Capture(diagnostics, SecretScope(diagnostics), pipe, CONDITION_SECONDS)
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    try:
        with short_directory() as base:
            async with served(base / "sockets", clock, source, capture) as (
                server,
                path,
            ):
                await malformed_request(path, clock)

                clock.advance(REVERIFY_NS)
                await until(lambda: diagnostics.state.snapshot().sink_failed)
                channel = await client(path, clock)
                try:
                    value = await asyncio.wait_for(
                        asked_status(channel, clock), CONDITION_SECONDS
                    )
                finally:
                    channel.close()

                assert value.status.state == "starting"
                await until(lambda: server.connections == 0)
            assert diagnostics.state.snapshot().sink_failures > 0
    finally:
        capture.scope.release()
        diagnostics.shutdown(CONDITION_SECONDS)
        pipe.close_write_end()

    assert not diagnostics.writer_is_alive()
    assert open_descriptors() == descriptors
    assert set(asyncio.all_tasks()) == tasks
