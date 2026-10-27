import asyncio
from datetime import timedelta
from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.vcs import Prepared, published
from kora.executor.entry import PublishOutcome
from kora.executor.launcher import ProcessLauncher
from kora.protocols.deadlines import ExecutionDeadline
from tests.plugins.vcs.git_support import (
    BARRIER_SECONDS,
    HeldHook,
    World,
    make_world,
    write,
)

REMAINING: timedelta = timedelta(milliseconds=500)


def prepared_in(world: World) -> Prepared:
    checkout = world.checkout()
    write(world.candidate(checkout), {"run.txt": "independent\n"})
    return world.prepare(checkout)


def deadline(world: World) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=world.clock.now() + REMAINING,
        scope_kind="attempt",
        scope_id="attempt",
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )


async def expiry_beside_a_stall(
    stall: HeldHook, held: World, expiring: World, first: Prepared, second: Prepared
) -> tuple[PublishOutcome, PublishOutcome, bool]:
    stuck = asyncio.create_task(published(held.executor, first, held.context()))
    await stall.entered()
    expired = await asyncio.wait_for(
        published(expiring.executor, second, expiring.context(deadline(expiring))),
        BARRIER_SECONDS,
    )
    still_held = not stuck.done()
    stall.proceed()
    released = await asyncio.wait_for(stuck, BARRIER_SECONDS)
    return released, expired, still_held


def test_integration_timeout_does_not_block_unrelated_runs(
    tmp_path: Path, kora_capture: Capture
) -> None:
    launcher = ProcessLauncher(kill_grace_ns=200_000_000, eof_bound_ns=500_000_000)
    (tmp_path / "held").mkdir()
    (tmp_path / "expiring").mkdir()
    held = make_world(tmp_path / "held", kora_capture, launcher=launcher)
    expiring = make_world(tmp_path / "expiring", kora_capture, launcher=launcher)
    first, second = prepared_in(held), prepared_in(expiring)

    with (
        HeldHook(held.remote, tmp_path / "held-hook") as stall,
        HeldHook(expiring.remote, tmp_path / "expiring-hook"),
    ):
        released, expired, still_held = asyncio.run(
            expiry_beside_a_stall(stall, held, expiring, first, second)
        )
        landed = expiring.remote_head()

    assert still_held
    assert expired.state == "unknown"
    assert landed is None
    assert released.state == "pushed"
    assert held.remote_head() == first.result.revision.commit
