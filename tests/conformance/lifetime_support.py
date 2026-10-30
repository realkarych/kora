import asyncio
import errno
import os
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, Sequence
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass
from io import FileIO
from pathlib import Path
from typing import Final, TypedDict, Unpack

from kora.conformance.bridge import finish_cleanup
from kora.conformance.controllers import duration_ns
from kora.conformance.io_helper import (
    ChildReaper,
    InstalledIOHelperLauncher,
    KernelChildReaper,
)
from kora.conformance.journal import JournalSnapshot
from kora.conformance.lifetime import ParentLifetime, RecoveryAttempts
from kora.protocols.lifetime import (
    CleanupReference,
    ClosureReport,
    LifetimeLimits,
    ProcessRegistration,
)
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import (
    ChannelError,
    ChannelFailure,
    DescriptorReadiness,
    wait_future,
)
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.runner.processes import (
    ExitAnchor,
    GatedProcess,
    RecoveredProcess,
    SignalResult,
)
from tests.conformance.lifetime_helpers import (
    CleanupDeadline,
    ProbeIOHelperLauncher,
    reap_once_reapable,
    release_recovered,
    unwind_owned,
)
from tests.conformance.lifetime_recovery_probe import (
    ControlledLifetimeClock,
    RecoveryProbeReport,
)
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import ProbeClock
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.reaper import (
    DeferredReaper,
)
from tests.support.event_loop import turn

RECOVERY_PROBE: Final = Path(__file__).with_name("lifetime_recovery_probe.py")
SENT_OR_SETTLED: Final = frozenset((SignalResult.SENT, SignalResult.SETTLED))


def limits() -> LifetimeLimits:
    return LifetimeLimits(
        cooperative_seconds=0.1,
        term_seconds=0.1,
        kill_observation_seconds=2.0,
        drain_seconds=1.0,
    )


def installed_launcher(home: Path) -> InstalledIOHelperLauncher:
    return InstalledIOHelperLauncher(home=home, state_home=home / "state")


async def close_recovered_test_owner(
    owner: ParentLifetime,
    clock: ControlledLifetimeClock,
    observers: Sequence[RecoveredProcess],
) -> ClosureReport:
    actual = SystemClock()
    closing = owner.request_close(None)
    clock.advance(duration_ns(owner.limits.cooperative_seconds) + 1)
    deadline = actual.monotonic_ns() + 5_000_000_000
    while not all(observer.observe(deadline).settled for observer in observers):
        assert actual.monotonic_ns() < deadline
        await turn()
    if observers and owner.cleanup_effect is None:
        clock.advance(duration_ns(owner.limits.term_seconds) + 1)
    return await asyncio.wait_for(
        asyncio.shield(closing),
        max(0, (deadline - actual.monotonic_ns()) / 1_000_000_000),
    )


async def dispose_test_owner(
    owner: ParentLifetime,
    clock: ControlledLifetimeClock | ProbeClock,
    cleanup: CleanupDeadline | None = None,
) -> None:
    closing = owner.request_close(None)
    clock.advance(40_000_000_000)
    deadline = clock.monotonic_ns() + 5_000_000_000
    remaining = (
        5.0
        if cleanup is None
        else max(0, (cleanup() - cleanup.clock.monotonic_ns()) / 1_000_000_000)
    )
    async with asyncio.timeout(remaining):
        for helper in tuple(owner.mutations.helpers):
            if not helper.closed:
                helper.signal(signal.SIGKILL)
                assert await helper.settle(deadline)
        await asyncio.shield(closing)
        dependencies = owner.recovery_dependencies()
        if dependencies:
            await asyncio.wait(dependencies)
        assert owner.inventory().cleanup_effect_tasks == 0
        assert owner.inventory().cleanup_driver_tasks == 0
        for record in owner.records.values():
            process = record.process or record.recovered
            if process is not None and not process.closed:
                process.signal(signal.SIGKILL, deadline)
                await process.settle(deadline)
                if isinstance(process, GatedProcess):
                    await process.drain(deadline)
                process.release(deadline)
            if record.directory is not None:
                record.directory.close()
        assert owner.inventory().retained_processes == 0
        assert owner.mutations.dispose_settled()


@asynccontextmanager
async def owned_lifetime(root: Path) -> AsyncGenerator[ParentLifetime]:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock, limits=limits(), launcher=installed_launcher(root)
    )
    try:
        await asyncio.wait_for(owner.ready(), 5)
        yield owner
    finally:
        await dispose_test_owner(owner, clock)


async def read_probe(descriptor: int, deadline: int, clock: Clock) -> bytes:
    readiness = DescriptorReadiness(descriptor, writing=False)
    try:
        await wait_future(readiness.ready, deadline, clock)
        return os.read(descriptor, 1)
    finally:
        readiness.close()


async def entered_fifo(
    directory: int, observation: int, deadline: int, clock: Clock
) -> FileIO:
    assert await read_probe(observation, deadline, clock) == b"r"
    with ExitStack() as pending:
        writer = pending.enter_context(
            FileIO(await open_fifo_writer(directory, deadline, clock), "wb")
        )
        assert await read_probe(observation, deadline, clock) == b"e"
        pending.pop_all()
        return writer


async def open_fifo_writer(directory: int, deadline: int, clock: Clock) -> int:
    while clock.monotonic_ns() < deadline:
        try:
            return os.open("stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=directory)
        except OSError as failure:
            if failure.errno != errno.ENXIO:
                raise
        await turn()
    raise TimeoutError("FIFO reader did not become ready")


class ProbeOptions(TypedDict, total=False):
    phase: str
    pause_at: int | tuple[int, ...] | None
    resist_term: bool
    reaper: ChildReaper


@dataclass(frozen=True)
class StallBarrier:
    home: Path
    directory: DirectoryAnchor
    reading: int
    writing: int

    def launcher(self, **options: Unpack[ProbeOptions]) -> ProbeIOHelperLauncher:
        return ProbeIOHelperLauncher(
            self.home, self.writing, self.directory.descriptor, **options
        )

    async def entered(self, deadline: int, clock: Clock) -> FileIO:
        return await entered_fifo(
            self.directory.descriptor, self.reading, deadline, clock
        )


def stall_barrier(resources: ExitStack, home: Path) -> StallBarrier:
    directory = DirectoryAnchor.open_path(home)
    resources.callback(directory.close)
    os.mkfifo("stall", 0o600, dir_fd=directory.descriptor)
    reading, writing = os.pipe()
    resources.callback(os.close, reading)
    resources.callback(os.close, writing)
    return StallBarrier(home, directory, reading, writing)


@dataclass(frozen=True)
class RetainedInvocation:
    reference: CleanupReference
    processes: tuple[RecoveredProcess, ...]
    cleanup: CleanupDeadline


async def read_handoff_bytes(
    descriptor: int,
    size: int,
    deadline: int,
    clock: Clock,
    entered: asyncio.Event | None = None,
) -> bytes:
    value = bytearray()
    while len(value) < size:
        readiness = DescriptorReadiness(descriptor, writing=False)
        try:
            await wait_future(readiness.ready, deadline, clock)
            chunk = os.read(descriptor, size - len(value))
        finally:
            readiness.close()
        if not chunk:
            raise ChannelError(ChannelFailure.CLOSED)
        value.extend(chunk)
        if entered is not None:
            entered.set()
    return bytes(value)


@asynccontextmanager
async def retained_live_invocation(
    home: Path,
    mode: str,
    *,
    clock: Clock | None = None,
    cleanup: CleanupDeadline | None = None,
    phase: str = "complete",
    entered: asyncio.Event | None = None,
    started: list[subprocess.Popen[bytes]] | None = None,
    cleanup_entered: asyncio.Event | None = None,
    cleanup_release: asyncio.Event | None = None,
) -> AsyncGenerator[RetainedInvocation]:
    actual = SystemClock() if clock is None else clock
    closing = CleanupDeadline(actual, 5_000_000_000) if cleanup is None else cleanup
    releases = AsyncExitStack()
    transferred = False

    async def release(process: RecoveredProcess) -> None:
        if transferred:
            observer = RecoveredProcess(
                process.registration, actual, NativeIdentityReader()
            )
            await release_recovered(observer, closing, actual)

    with ExitStack() as handles:
        process = subprocess.Popen(
            [sys.executable, "-I", str(RECOVERY_PROBE), str(home), mode, phase],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            bufsize=0,
        )
        handles.callback(process.poll)
        if started is not None:
            started.append(process)
        if process.stdin is not None:
            handles.callback(process.stdin.close)
        if process.stdout is not None:
            handles.callback(process.stdout.close)
        anchor = ExitAnchor(process.pid)
        handles.callback(anchor.close)
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            descriptor = process.stdout.fileno()
            os.set_blocking(descriptor, False)
            deadline = actual.monotonic_ns() + 30_000_000_000
            size = int.from_bytes(
                await read_handoff_bytes(descriptor, 4, deadline, actual), "big"
            )
            if size > 65536:
                raise ChannelError(ChannelFailure.CAPACITY)
            if phase == "before_report":
                deadline = actual.monotonic_ns()
            encoded = await read_handoff_bytes(
                descriptor,
                size,
                deadline,
                actual,
                entered if phase == "partial" else None,
            )
            report = RecoveryProbeReport.model_validate_json(encoded)
            assert report.mode == mode
            recovered: list[RecoveredProcess] = []
            for registration in report.registrations:
                owner = RecoveredProcess(registration, actual, NativeIdentityReader())
                releases.callback(owner.dispose_observation)
                releases.push_async_callback(release, owner)
                recovered.append(owner)
            if phase == "cancel_before_ack":
                assert entered is not None
                entered.set()
                waiting: asyncio.Future[None] = (
                    asyncio.get_running_loop().create_future()
                )
                await waiting
            if phase == "ack_failure":
                process.stdin.close()
            assert os.write(process.stdin.fileno(), b"a") == 1
            transferred = True
            process.stdin.close()
            await anchor.wait(deadline, actual)
            process.returncode = await reap_once_reapable(
                KernelChildReaper(), process.pid, deadline, actual
            )
            assert process.returncode == 0
            yield RetainedInvocation(
                CleanupReference(invocation_id=report.invocation_id),
                tuple(recovered),
                closing,
            )
        finally:
            original = sys.exception()

            async def close() -> None:
                deadline = closing()
                try:
                    if cleanup_entered is not None:
                        cleanup_entered.set()
                    if cleanup_release is not None:
                        await asyncio.wait_for(
                            cleanup_release.wait(),
                            max(0, (deadline - actual.monotonic_ns()) / 1_000_000_000),
                        )
                finally:
                    try:
                        if process.stdin is not None:
                            process.stdin.close()
                        if process.returncode is None:
                            await anchor.wait(deadline, actual)
                            process.returncode = await reap_once_reapable(
                                KernelChildReaper(), process.pid, deadline, actual
                            )
                            assert process.returncode == 0
                    finally:
                        await unwind_owned(releases)

            await finish_cleanup(close, original)


def assert_recovered_processes_absent(processes: Sequence[RecoveredProcess]) -> None:
    reader = NativeIdentityReader()
    for process in processes:
        assert (
            reader.read(process.registration.process.pid).state is IdentityState.ABSENT
        )
        group = reader.group(process.registration.process_group_id)
        assert group.state is IdentityState.PRESENT
        assert group.members == ()
        assert process.watcher_count == 0


def retained_invocation(home: Path, mode: str) -> CleanupReference:
    probe = subprocess.run(
        [sys.executable, "-I", str(RECOVERY_PROBE), str(home), mode],
        check=True,
        capture_output=True,
    )
    return CleanupReference(invocation_id=probe.stdout.decode("ascii"))


def journal_file(home: Path, invocation_id: str) -> Path:
    return next(home.rglob(invocation_id)) / "journal"


def retained_journal(home: Path, invocation_id: str) -> tuple[Path, JournalSnapshot]:
    path = journal_file(home, invocation_id)
    return path, JournalSnapshot.model_validate_json(path.read_bytes())


def independent_process(journal: JournalSnapshot, clock: Clock) -> RecoveredProcess:
    registration = journal.entries[-1].registration
    assert isinstance(registration, ProcessRegistration)
    return RecoveredProcess(registration, clock, NativeIdentityReader())


async def kill_independent(
    process: RecoveredProcess, deadline: int, accepted: frozenset[SignalResult]
) -> None:
    assert process.signal(signal.SIGKILL, deadline) in accepted
    await process.settle(deadline)
    process.release(deadline)


async def recovery_progress(caller: asyncio.Task[ClosureReport], deadline: int) -> None:
    if caller.done():
        result = caller.result()
        raise AssertionError(f"recovery completed before frontier: {result.state}")
    assert SystemClock().monotonic_ns() < deadline
    await turn()


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


async def reap_watched(reaper: DeferredReaper, pid: int) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    while (held := reaper.held.get(pid)) is None or not held.watches:
        assert clock.monotonic_ns() < deadline
        await turn()
