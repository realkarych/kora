import asyncio
import os
from contextlib import ExitStack, nullcontext
from pathlib import Path

import pytest

from kora.cli import CLI_COMPONENT
from kora.conformance.capture import captured
from kora.conformance.command import ConformanceCommand
from kora.conformance.common import utc_now
from kora.conformance.result import ChildInput
from kora.observability import LoggingSettings
from kora.protocols.lifetime import CleanupOutcome, OriginalOutcome
from kora.protocols.metadata import ProtocolName
from kora.protocols.time import SystemClock
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    dispose_test_owner,
    read_probe,
    stall_barrier,
)
from tests.support.event_loop import turn


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "deadline", "setup_failure"])
async def test_command_owns_interrupted_listener_receipt_and_startup_deadline(
    tmp_path: Path, mode: str
) -> None:
    clock = ControlledLifetimeClock()
    actual = SystemClock()
    with ExitStack() as resources:
        capture = resources.enter_context(
            captured(CLI_COMPONENT, LoggingSettings(), "command-deadline", utc_now)
        )
        barrier = stall_barrier(resources, tmp_path)
        launcher = barrier.launcher(phase="after", pause_at=(1, 7))
        command = ConformanceCommand(
            capture.diagnostics, clock=clock, launcher=launcher
        )
        request = ChildInput(
            protocol=ProtocolName.SECRETS, name="env", fixtures="unused.fixture"
        )
        operation = asyncio.create_task(command.run(request))
        expected = (
            pytest.raises(RuntimeError, match="command setup interrupted")
            if mode == "setup_failure"
            else nullcontext()
        )
        with expected:
            try:
                async with asyncio.timeout(10):
                    if mode == "setup_failure":
                        assert (
                            await read_probe(
                                barrier.reading,
                                actual.monotonic_ns() + 5_000_000_000,
                                actual,
                            )
                            == b"r"
                        )
                        raise RuntimeError("command setup interrupted")
                    with await barrier.entered(
                        actual.monotonic_ns() + 5_000_000_000, actual
                    ):
                        clock.advance(15_000_000_000)
                    with await barrier.entered(
                        actual.monotonic_ns() + 5_000_000_000, actual
                    ):
                        assert launcher.starts == 7
                        helper = command.owner.mutations.helpers[-1]
                        descriptors = helper.descriptors
                        assert len(descriptors) == 3
                        assert not helper.closed
                        assert not command.listeners

                        if mode == "cancel":
                            operation.cancel()
                        else:
                            clock.advance(16_000_000_000)
                        while helper.descriptors:
                            await turn()
                        for descriptor in descriptors:
                            with pytest.raises(OSError, match="Bad file descriptor"):
                                os.fstat(descriptor)
                    report = await operation

                    assert helper.closed
                    assert helper.process.returncode is not None
                    assert report.outcome is (
                        OriginalOutcome.CANCELLED
                        if mode == "cancel"
                        else OriginalOutcome.FAILED
                    )
                    assert report.cleanup is not None
                    assert report.cleanup.state is CleanupOutcome.UNKNOWN
                    assert command.owner.inventory().helpers.retained_helpers == 0
                    assert command.process is None
                    assert launcher.starts == 9
            finally:
                operation.cancel()
                try:
                    await dispose_test_owner(command.owner, clock)
                finally:
                    await asyncio.wait_for(asyncio.shield(operation), 5)

        assert operation.done()
        assert command.owner.inventory().helpers.retained_helpers == 0
        assert command.owner.inventory().helpers.live_helpers == 0
