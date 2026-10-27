import asyncio
import os
from contextlib import ExitStack
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
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_command_owns_interrupted_listener_receipt_and_startup_deadline(
    tmp_path: Path, mode: str
) -> None:
    clock = ControlledLifetimeClock()
    actual = SystemClock()
    with ExitStack() as resources:
        capture = resources.enter_context(
            captured(CLI_COMPONENT, LoggingSettings(), "command-deadline", utc_now)
        )
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(
            tmp_path, writing, barrier.descriptor, phase="after", pause_at=(1, 7)
        )
        command = ConformanceCommand(
            capture.diagnostics, clock=clock, launcher=launcher
        )
        request = ChildInput(
            protocol=ProtocolName.SECRETS, name="env", fixtures="unused.fixture"
        )
        operation = asyncio.create_task(command.run(request))
        try:
            async with asyncio.timeout(10):
                with await entered_fifo(
                    barrier.descriptor,
                    reading,
                    actual.monotonic_ns() + 5_000_000_000,
                    actual,
                ):
                    clock.advance(15_000_000_000)
                with await entered_fifo(
                    barrier.descriptor,
                    reading,
                    actual.monotonic_ns() + 5_000_000_000,
                    actual,
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
                    loop = asyncio.get_running_loop()
                    while helper.descriptors:
                        turn: asyncio.Future[None] = loop.create_future()
                        loop.call_soon(turn.set_result, None)
                        await turn
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
            clock.advance(40_000_000_000)
            await asyncio.gather(operation, return_exceptions=True)
            await dispose_test_owner(command.owner, clock)
