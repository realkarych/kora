import asyncio
import gc
import os
import signal
import sys
from contextlib import ExitStack
from io import FileIO
from pathlib import Path

import pytest

from kora.conformance.lifetime import LifetimeError, RecoveryAttempt, RecoveryAttempts
from kora.protocols.lifetime import CleanupOutcome, LifetimeReason
from kora.protocols.time import SystemClock
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    StallBarrier,
    limits,
    recovery_progress,
    retained_invocation,
    settle_recovery_attempts,
    stall_barrier,
)


async def retain(
    home: Path,
    registry: RecoveryAttempts,
    launcher: ProbeIOHelperLauncher,
    clock: ControlledLifetimeClock,
    barrier: StallBarrier,
) -> tuple[RecoveryAttempt, FileIO]:
    reference = retained_invocation(home, "tree")
    caller = asyncio.create_task(
        registry.resume(
            reference,
            limits(),
            launcher=launcher,
            home=str(home),
            state_home=str(home / "state"),
            clock=clock,
        )
    )
    deadline = SystemClock().monotonic_ns() + 5_000_000_000
    while launcher.starts < 2:
        await recovery_progress(caller, deadline)
    context = registry.context
    assert context is not None
    attempt = context.attempts[reference.invocation_id]
    owner = attempt.owner
    assert owner is not None
    helper = owner.mutations.helpers[-1]
    with ExitStack() as pending:
        writer = pending.enter_context(await barrier.entered(deadline, SystemClock()))
        while not helper.descriptors:
            await recovery_progress(caller, deadline)
        clock.advance(int(owner.limits.acquisition_seconds * 1_000_000_000) + 1)
        await asyncio.wait_for(owner.closure_started.wait(), 5)
        clock.advance(
            int(
                (
                    owner.limits.cooperative_seconds
                    + owner.limits.term_seconds
                    + owner.limits.kill_observation_seconds
                    + owner.limits.drain_seconds
                )
                * 1_000_000_000
            )
            + 1
        )
        report = await asyncio.wait_for(caller, 5)
        assert report.state is CleanupOutcome.UNKNOWN
        assert registry.inventory().completed_tasks == 1
        pending.pop_all()
        return attempt, writer


async def refuse(
    home: Path,
    registry: RecoveryAttempts,
    attempt: RecoveryAttempt,
    launcher: ProbeIOHelperLauncher,
    clock: ControlledLifetimeClock,
) -> None:
    with pytest.raises(LifetimeError) as refused:
        await registry.resume(
            attempt.reference,
            attempt.limits,
            launcher=launcher,
            home=str(home),
            state_home=str(home / "state"),
            clock=clock,
        )
    assert refused.value.reason is LifetimeReason.IDENTITY_UNKNOWN


def main(home: Path) -> None:
    with ExitStack() as resources:
        barrier = stall_barrier(resources, home)
        launcher = barrier.launcher(phase="after", pause_at=2)
        clock = ControlledLifetimeClock()
        registry = RecoveryAttempts()
        loop = asyncio.new_event_loop()
        try:
            attempt, writer = loop.run_until_complete(
                retain(home, registry, launcher, clock, barrier)
            )
            resources.enter_context(writer)
            loop.close()
            gc.collect()
            owner = attempt.owner
            assert owner is not None
            helper = owner.mutations.helpers[-1]
            claim = owner.mutations.claim
            assert claim is not None
            before = os.fstat(claim)

            asyncio.run(refuse(home, registry, attempt, launcher, clock))
            writer.close()
            assert helper.process.wait(timeout=5) == 0
            asyncio.run(refuse(home, registry, attempt, launcher, clock))

            assert registry.context is attempt.context
            assert registry.inventory().completed_tasks == 1
            assert registry.inventory().dependency_callbacks == 1
            assert not helper.closed
            assert not helper.anchor.exited
            assert os.fstat(claim) == before
            assert launcher.starts == 2
        finally:
            if not loop.is_closed():
                try:
                    loop.run_until_complete(settle_recovery_attempts(registry, clock))
                finally:
                    loop.close()
            context = registry.context
            if context is not None:
                for residual in tuple(context.attempts.values()):
                    if residual.owner is not None:
                        for helper in residual.owner.mutations.helpers:
                            if helper.process.poll() is None:
                                helper.process.send_signal(signal.SIGKILL)
                            helper.process.wait(timeout=5)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
