import time
from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import checked_out, published, run
from kora.executor.captures import Captures
from kora.executor.failures import ExecutorError
from kora.executor.guard import PhaseGuard
from kora.executor.launcher import ProcessLauncher
from kora.executor.steps import run_step
from kora.ir.node import ErrorClass
from kora.protocols.deadlines import ExecutionDeadline, LocalThresholds, duration_ns
from kora.protocols.time import SystemClock
from tests.executor.step_support import context, step
from tests.plugins.vcs.git_support import (
    REPO,
    ManualClock,
    World,
    make_world,
    write,
)
from tests.plugins.vcs.launchers import RecordingLauncher

REMAINING: timedelta = timedelta(seconds=20)


def deadline(world: World, remaining: timedelta) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=world.clock.now() + remaining,
        scope_kind="attempt",
        scope_id="attempt",
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )


def test_managed_phases_share_effective_deadline(
    tmp_path: Path, kora_capture: Capture
) -> None:
    launcher = RecordingLauncher()
    world = make_world(tmp_path, kora_capture, launcher=launcher, clock=ManualClock())
    context = world.context(deadline(world, REMAINING))
    checkout = run(
        checked_out(
            world.provider,
            world.executor,
            world.bench().areas(),
            REPO,
            world.base,
            context,
        )
    )
    write(world.candidate(checkout), {"bounded.txt": "bounded\n"})
    prepared = world.prepare(checkout, ctx=context)

    outcome = run(published(world.executor, prepared, context))

    bound = duration_ns(REMAINING)
    assert outcome.state == "pushed"
    assert launcher.specs
    assert all(spec.timeout_ns <= bound for spec in launcher.specs)


def test_a_due_interruption_cancels_the_active_step(
    tmp_path: Path, kora_capture: Capture
) -> None:
    launcher = ProcessLauncher(kill_grace_ns=200_000_000, eof_bound_ns=500_000_000)
    world = make_world(tmp_path, kora_capture, launcher=launcher, clock=ManualClock())
    hook = world.remote / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexec /bin/sleep 3\n")
    hook.chmod(0o755)
    checkout = world.checkout()
    write(world.candidate(checkout), {"slow.txt": "slow\n"})
    prepared = world.prepare(checkout)
    started = time.monotonic()

    outcome = run(
        published(
            world.executor,
            prepared,
            world.context(deadline(world, timedelta(milliseconds=500))),
        )
    )

    assert outcome.state == "unknown"
    assert time.monotonic() - started < 2.5


def test_bounded_diagnostics_do_not_delay_escalation(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = SystemClock()
    now = clock.monotonic_ns()
    guard = PhaseGuard(clock, LocalThresholds(now + 500_000_000, now + 500_000_000))
    launcher = ProcessLauncher(kill_grace_ns=200_000_000, eof_bound_ns=500_000_000)
    subject = context(kora_capture, tmp_path, guard=guard, launcher=launcher)
    noisy = step(
        "import sys\nwhile True: sys.stderr.write('x' * 65536)",
        timeout=timedelta(hours=1),
    )
    started = time.monotonic()

    with pytest.raises(ExecutorError) as raised:
        run(run_step(noisy, 0, subject, Captures()))

    assert raised.value.failure_class is ErrorClass.TIMEOUT
    assert time.monotonic() - started < 2.5


def test_a_zero_bound_fails_checkout_with_timeout(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture, clock=ManualClock())

    with pytest.raises(ExecutorError) as raised:
        run(
            checked_out(
                world.provider,
                world.executor,
                world.bench().areas(),
                REPO,
                world.base,
                world.context(deadline(world, timedelta(0))),
            )
        )

    assert raised.value.failure_class is ErrorClass.TIMEOUT
