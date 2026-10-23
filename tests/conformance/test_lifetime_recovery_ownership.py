import asyncio
import fcntl
import gc
import os
import queue
import signal
import subprocess
import sys
import threading
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.lifetime import LifetimeError, RecoveryAttempts
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
    ClosureReport,
    LifetimeReason,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


async def recovery_progress(caller: asyncio.Task[ClosureReport], deadline: int) -> None:
    if caller.done():
        result = caller.result()
        raise AssertionError(f"recovery completed before frontier: {result.state}")
    assert SystemClock().monotonic_ns() < deadline
    loop = asyncio.get_running_loop()
    wake: asyncio.Future[None] = loop.create_future()
    loop.call_soon(wake.set_result, None)
    await wake


async def settle_recovery_attempts(
    attempts: RecoveryAttempts, clock: ControlledLifetimeClock
) -> None:
    context = attempts.context
    if context is None:
        return
    for attempt in tuple(context.attempts.values()):
        owner = attempt.owner
        if owner is None:
            continue
        closing = owner.request_close(None)
        clock.advance(
            int(
                (
                    owner.limits.acquisition_seconds
                    + owner.limits.cooperative_seconds
                    + owner.limits.term_seconds
                    + owner.limits.kill_observation_seconds
                    + owner.limits.drain_seconds
                )
                * 1_000_000_000
            )
            + 1
        )
        for helper in tuple(owner.mutations.helpers):
            if not helper.closed:
                helper.signal(signal.SIGKILL)
                await asyncio.wait_for(
                    helper.settle(clock.monotonic_ns() + 5_000_000_000), 5
                )
        await asyncio.wait_for(asyncio.shield(closing), 5)
        if attempt.task is not None:
            await asyncio.wait_for(asyncio.shield(attempt.task), 5)
        attempt.reconcile()


def test_closed_loop_retains_unknown_recovery_until_actual_process_exit(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.conformance.lifetime_recovery_loop_probe",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        cwd=Path(__file__).parents[2],
    )

    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.asyncio
async def test_cancelled_recovery_retains_exact_attempt_through_gc_and_join(
    tmp_path: Path,
) -> None:
    original = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "tree",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=original.stdout.decode("ascii"))
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(
            tmp_path, writing, barrier.descriptor, phase="after", pause_at=1
        )
        clock = ControlledLifetimeClock()
        attempts = RecoveryAttempts()
        try:
            caller = asyncio.create_task(
                attempts.resume(
                    reference,
                    limits(),
                    launcher=launcher,
                    home=str(tmp_path),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
            )
            deadline = SystemClock().monotonic_ns() + 5_000_000_000
            while not launcher.starts:
                await recovery_progress(caller, deadline)
            context = attempts.context
            assert context is not None
            attempt = context.attempts[reference.invocation_id]
            owner = attempt.owner
            assert owner is not None
            helper = owner.mutations.helpers[-1]
            await entered_fifo(helper.process.pid, reading, deadline, SystemClock())

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            del caller
            gc.collect()
            claim_path = next(tmp_path.rglob(reference.invocation_id)) / "claim"
            claim = os.open(claim_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(claim)
            with pytest.raises(LifetimeError) as conflicting:
                await attempts.resume(
                    reference,
                    limits().model_copy(update={"max_state_root_invocations": 1}),
                    launcher=launcher,
                    home=str(tmp_path),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
            assert conflicting.value.reason is LifetimeReason.IDENTITY_MISMATCH
            with pytest.raises(LifetimeError) as unbound:
                await attempts.resume(
                    CleanupReference(invocation_id="f" * 32),
                    limits(),
                    launcher=launcher,
                    home=str(tmp_path),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
            assert unbound.value.reason is LifetimeReason.IDENTITY_UNKNOWN
            with pytest.raises(LifetimeError) as foreign:
                await attempts.resume(
                    reference,
                    limits(),
                    launcher=launcher,
                    home=str(tmp_path / "other"),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
            assert foreign.value.reason is LifetimeReason.IDENTITY_UNKNOWN
            with pytest.raises(LifetimeError) as loop_refused:
                await asyncio.to_thread(
                    asyncio.run,
                    attempts.resume(
                        reference,
                        limits(),
                        launcher=launcher,
                        home=str(tmp_path),
                        state_home=str(tmp_path / "state"),
                        clock=clock,
                    ),
                )
            assert loop_refused.value.reason is LifetimeReason.IDENTITY_UNKNOWN
            assert launcher.starts == 1
            assert len(context.attempts) == 1
            joining = asyncio.create_task(
                attempts.resume(
                    reference,
                    limits(),
                    launcher=launcher,
                    home=str(tmp_path),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
            )
            writer = os.open(
                "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
            )
            os.close(writer)
            report = await asyncio.wait_for(joining, 10)

            assert report.state is CleanupOutcome.CLOSED
            assert owner.generation == 2
            assert helper.closed
            assert helper.process.returncode == 0
            assert attempt.task is not None
            assert attempt.task.done()
            assert attempts.context is None
            assert not attempt.dependencies
            assert owner.mutations.claim is None
            assert owner.mutations.inventory().directory_descriptors == 0
        finally:
            await settle_recovery_attempts(attempts, clock)


@pytest.mark.asyncio
async def test_recovery_construction_refusal_releases_exact_admission(
    tmp_path: Path,
) -> None:
    from kora.conformance.io_helper import InstalledIOHelperLauncher

    attempts = RecoveryAttempts()
    launcher = InstalledIOHelperLauncher(home=tmp_path, state_home=tmp_path / "state")
    reference = CleanupReference(invocation_id="e" * 32)
    refused_limits = limits().model_copy(update={"max_control_recovery_bytes": 1})

    with pytest.raises(LifetimeError) as refused:
        await attempts.resume(
            reference,
            refused_limits,
            launcher=launcher,
            home=launcher.home,
            state_home=launcher.state_home,
        )

    assert refused.value.reason is LifetimeReason.CAPACITY_EXCEEDED
    assert attempts.context is None
    assert not tuple(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_completed_unknown_recovery_retains_helper_until_positive_exit(
    tmp_path: Path,
) -> None:
    original = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "tree",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=original.stdout.decode("ascii"))
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(
            tmp_path, writing, barrier.descriptor, phase="after", pause_at=2
        )
        clock = ControlledLifetimeClock()
        attempts = RecoveryAttempts()
        try:
            caller = asyncio.create_task(
                attempts.resume(
                    reference,
                    limits(),
                    launcher=launcher,
                    home=str(tmp_path),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
            )
            loop = asyncio.get_running_loop()
            deadline = SystemClock().monotonic_ns() + 5_000_000_000
            while launcher.starts < 2:
                await recovery_progress(caller, deadline)
            context = attempts.context
            assert context is not None
            attempt = context.attempts[reference.invocation_id]
            owner = attempt.owner
            assert owner is not None
            helper = owner.mutations.helpers[-1]
            await entered_fifo(helper.process.pid, reading, deadline, SystemClock())
            retained = path.read_bytes()
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
            gc.collect()
            with pytest.raises(LifetimeError) as capacity:
                await attempts.resume(
                    CleanupReference(invocation_id="f" * 32),
                    limits().model_copy(update={"max_state_root_invocations": 1}),
                    launcher=launcher,
                    home=str(tmp_path),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
            assert capacity.value.reason is LifetimeReason.CAPACITY_EXCEEDED
            assert (
                await attempts.resume(
                    reference,
                    limits(),
                    launcher=launcher,
                    home=str(tmp_path),
                    state_home=str(tmp_path / "state"),
                    clock=clock,
                )
                is report
            )
            assert report.state is CleanupOutcome.UNKNOWN
            assert (
                context.root == owner.mutations.root.identity
                if owner.mutations.root is not None
                else False
            )
            assert attempts.context is context
            assert not helper.closed
            assert helper.process.returncode is None
            assert len(attempt.dependencies) == 1
            assert launcher.starts == 2
            assert path.read_bytes() == retained
            writer = os.open(
                "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
            )
            os.close(writer)
            actual_clock = SystemClock()
            deadline = actual_clock.monotonic_ns() + 5_000_000_000
            await helper.anchor.wait(deadline, actual_clock)
            while attempts.context is not None:
                assert actual_clock.monotonic_ns() < deadline
                wake = loop.create_future()
                loop.call_soon(wake.set_result, None)
                await wake

            assert helper.closed
            assert helper.process.returncode == 0
            assert owner.mutations.claim is None
            assert owner.mutations.inventory().directory_descriptors == 0
            assert not attempt.dependencies
            assert path.read_bytes() == retained
            assert report.state is CleanupOutcome.UNKNOWN
            claim = os.open(path.parent / "claim", os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(claim)
        finally:
            await settle_recovery_attempts(attempts, clock)


def concurrent_resume(
    gate: threading.Barrier,
    results: queue.Queue[ClosureReport | LifetimeReason],
    registry: RecoveryAttempts,
    launcher: ProbeIOHelperLauncher,
    reference: CleanupReference,
    home: Path,
) -> None:
    gate.wait(timeout=5)
    try:
        result = asyncio.run(
            registry.resume(
                reference,
                limits(),
                launcher=launcher,
                home=str(home),
                state_home=str(home / "state"),
            )
        )
    except LifetimeError as error:
        results.put(error.reason)
    else:
        results.put(result)


@pytest.mark.asyncio
async def test_simultaneous_thread_recovery_has_one_initial_effect_owner(
    tmp_path: Path,
) -> None:
    references: list[CleanupReference] = []
    for _ in range(2):
        previous = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("lifetime_recovery_probe.py")),
                str(tmp_path),
                "tree",
            ],
            check=True,
            capture_output=True,
        )
        references.append(
            CleanupReference(invocation_id=previous.stdout.decode("ascii"))
        )
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(
            tmp_path, writing, barrier.descriptor, phase="after", pause_at=1
        )
        registry = RecoveryAttempts()
        gate = threading.Barrier(3)
        results: queue.Queue[ClosureReport | LifetimeReason] = queue.Queue()
        threads = [
            threading.Thread(
                target=concurrent_resume,
                args=(gate, results, registry, launcher, reference, tmp_path),
            )
            for reference in references
        ]
        try:
            for thread in threads:
                thread.start()
            gate.wait(timeout=5)
            clock = SystemClock()
            deadline = clock.monotonic_ns() + 5_000_000_000
            loop = asyncio.get_running_loop()
            while not launcher.starts:
                assert clock.monotonic_ns() < deadline
                assert any(thread.is_alive() for thread in threads)
                wake: asyncio.Future[None] = loop.create_future()
                loop.call_soon(wake.set_result, None)
                await wake
            context = registry.context
            assert context is not None
            attempt = next(iter(context.attempts.values()))
            owner = attempt.owner
            assert owner is not None
            while not owner.mutations.helpers:
                assert clock.monotonic_ns() < deadline
                assert any(thread.is_alive() for thread in threads)
                wake = loop.create_future()
                loop.call_soon(wake.set_result, None)
                await wake
            helper = owner.mutations.helpers[-1]
            await entered_fifo(helper.process.pid, reading, deadline, clock)

            refused = await asyncio.to_thread(results.get, True, 5)
            assert refused is LifetimeReason.IDENTITY_UNKNOWN
            assert launcher.starts == 1
            assert registry.inventory().attempts == 1
            writer = os.open(
                "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
            )
            os.close(writer)
            report = await asyncio.to_thread(results.get, True, 5)

            assert isinstance(report, ClosureReport)
            assert report.state is CleanupOutcome.CLOSED
            assert helper.closed
            assert helper.process.returncode == 0
            assert registry.context is None
            assert owner.generation == 2
            other = next(
                reference
                for reference in references
                if reference.invocation_id != owner.invocation_id
            )
            from kora.conformance.journal import JournalSnapshot

            retained = JournalSnapshot.model_validate_json(
                (next(tmp_path.rglob(other.invocation_id)) / "journal").read_bytes()
            )
            assert retained.generation == 1
        finally:
            context = registry.context
            if context is not None:
                for attempt in tuple(context.attempts.values()):
                    if attempt.owner is not None:
                        for helper in tuple(attempt.owner.mutations.helpers):
                            if helper.process.poll() is None:
                                helper.process.send_signal(signal.SIGKILL)
            for thread in threads:
                if thread.ident is not None:
                    await asyncio.to_thread(thread.join, 5)
                    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_failed_recovery_frontier_propagates_and_disposes_owned_attempt(
    tmp_path: Path,
) -> None:
    from kora.conformance.io_helper import InstalledIOHelperLauncher

    registry = RecoveryAttempts()
    launcher = InstalledIOHelperLauncher(home=tmp_path, state_home=tmp_path / "state")
    clock = ControlledLifetimeClock()
    caller = asyncio.create_task(
        registry.resume(
            CleanupReference(invocation_id="d" * 32),
            limits(),
            launcher=launcher,
            home=launcher.home,
            state_home=launcher.state_home,
            clock=clock,
        )
    )
    deadline = SystemClock().monotonic_ns() + 5_000_000_000
    try:
        while not caller.done():
            await recovery_progress(caller, deadline)
        with pytest.raises(AssertionError, match="recovery completed before frontier"):
            await recovery_progress(caller, deadline)

        assert caller.done()
        assert caller.result().state is CleanupOutcome.UNKNOWN
        assert registry.context is None
        assert registry.inventory().attempts == 0
        assert registry.inventory().dependency_callbacks == 0
    finally:
        await settle_recovery_attempts(registry, clock)
