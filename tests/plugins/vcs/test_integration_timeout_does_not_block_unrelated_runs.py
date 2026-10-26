import asyncio
from datetime import timedelta
from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.vcs import Prepared, published
from kora.executor.entry import PublishOutcome
from kora.executor.launcher import ProcessLauncher
from kora.protocols.deadlines import ExecutionDeadline
from tests.plugins.vcs.git_support import ManualClock, World, make_world, write

STALL_SECONDS: int = 3


def stalled_remote(world: World) -> None:
    hook = world.remote / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\nexec /bin/sleep {STALL_SECONDS}\n")
    hook.chmod(0o755)


def held(world: World) -> Prepared:
    checkout = world.checkout()
    write(world.candidate(checkout), {"run.txt": "independent\n"})
    return world.prepare(checkout)


async def both(
    stalled: World, healthy: World, first: Prepared, second: Prepared
) -> tuple[PublishOutcome, PublishOutcome]:
    clock = stalled.clock
    assert isinstance(clock, ManualClock)
    deadline = ExecutionDeadline(
        at=clock.now() + timedelta(seconds=1),
        scope_kind="attempt",
        scope_id="attempt",
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )
    return await asyncio.gather(
        published(stalled.executor, first, stalled.context(deadline)),
        published(healthy.executor, second, healthy.context()),
    )


def test_integration_timeout_does_not_block_unrelated_runs(
    tmp_path: Path, kora_capture: Capture
) -> None:
    launcher = ProcessLauncher(kill_grace_ns=200_000_000, eof_bound_ns=500_000_000)
    (tmp_path / "stalled").mkdir()
    (tmp_path / "healthy").mkdir()
    stalled = make_world(tmp_path / "stalled", kora_capture, launcher=launcher)
    healthy = make_world(tmp_path / "healthy", kora_capture, launcher=launcher)
    first, second = held(stalled), held(healthy)
    stalled_remote(stalled)

    stuck, completed = asyncio.run(both(stalled, healthy, first, second))

    assert stuck.state == "unknown"
    assert completed.state == "pushed"
    assert healthy.remote_head() == second.result.revision.commit
