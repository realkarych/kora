import asyncio
import os
from pathlib import Path

import pytest

from kora.conformance.lifetime import ParentLifetime, RecoveryAttempts
from kora.protocols.lifetime import CleanupOutcome, new_operation_id
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits
from tests.measurements.lifetime_measurement_support import MeasuredIOHelperLauncher
from tests.measurements.lifetime_observation import MeasuredLoop, Observer
from tests.measurements.lifetime_workload import establish, launch_value


def test_measurement_settlement_checks_retained_identity_without_owner(
    tmp_path: Path,
) -> None:
    async def exercise(loop: MeasuredLoop) -> None:
        descriptor = os.open(
            tmp_path / "helpers", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        launcher = MeasuredIOHelperLauncher(tmp_path, descriptor)
        observer = Observer(tmp_path / "points", loop, RecoveryAttempts(), launcher)
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(clock=clock, limits=limits(), launcher=launcher)
        try:
            async with asyncio.timeout(10):
                tree, child = await establish(owner)
                await owner.acquire(
                    owner.records[child].token,
                    new_operation_id(),
                    launch_value(tree, "", "shutdown", False),
                )
                observer.owner = owner
                observer.track_owner()
                observer.owner = None

                with pytest.raises(AssertionError):
                    await observer.settled()
                held = observer.point()
                closed = await owner.close(None)
                settled = await observer.settled()

                assert held.tracked_processes > held.absent_processes
                assert held.child_rss
                assert closed.state is CleanupOutcome.CLOSED
                assert settled.tracked_processes > 0
                assert settled.absent_processes == settled.tracked_processes
                assert settled.unknown_processes == 0
                assert not settled.child_rss
        finally:
            try:
                await dispose_test_owner(owner, clock)
            finally:
                await observer.close()
                os.close(descriptor)

    loop = MeasuredLoop()
    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise(loop))
