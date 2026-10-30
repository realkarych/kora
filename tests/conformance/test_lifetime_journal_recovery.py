import asyncio
import gc
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.journal import JournalSnapshot
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
    ProcessRegistration,
    ResourceKind,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.identity import (
    GroupObservation,
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
)
from kora.runner.processes import ExitAnchor, RecoveredProcess, SignalResult
from tests.conformance.lifetime_helpers import (
    CleanupDeadline,
    join_helper_operation,
    turn,
)
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    assert_recovered_processes_absent,
    dispose_test_owner,
    journal_file,
    limits,
    retained_live_invocation,
)


async def reap_recovered(
    processes: Sequence[RecoveredProcess],
    clock: SystemClock,
    accepted: tuple[SignalResult, ...],
    cleanup: CleanupDeadline,
) -> None:
    if not processes:
        return
    process, *remaining = processes
    try:
        deadline = cleanup()
        assert process.signal(signal.SIGKILL, deadline) in accepted
        await process.settle(deadline)
        process.release(deadline)
    finally:
        await reap_recovered(remaining, clock, accepted, cleanup)


@pytest.mark.asyncio
async def test_journal_recovery_reaps_siblings_after_a_cleanup_assertion(
    tmp_path: Path,
) -> None:
    async with retained_live_invocation(tmp_path, "pair") as acquired:
        reference = acquired.reference
        path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
        journal = JournalSnapshot.model_validate_json(path.read_bytes())
        clock = SystemClock()
        registrations = [
            entry.registration
            for entry in journal.entries
            if isinstance(entry.registration, ProcessRegistration)
        ]
        processes = list(acquired.processes)
        anchors: list[ExitAnchor] = []
        exited: list[bool] = []

        try:
            for registration in registrations:
                anchors.append(ExitAnchor(registration.process.pid, direct_child=False))
            with pytest.raises(AssertionError, match=r"in \(\)"):
                await reap_recovered(processes, clock, (), acquired.cleanup)
            deadline = acquired.cleanup()
            for anchor in anchors:
                await anchor.wait(deadline, clock)
            exited = [anchor.exited for anchor in anchors]
        finally:
            deadline = acquired.cleanup()
            try:
                for process in processes:
                    process.signal(signal.SIGKILL, deadline)
                for anchor in anchors:
                    await anchor.wait(deadline, clock)
                for process in processes:
                    while not process.observe(deadline).settled:
                        assert clock.monotonic_ns() < deadline
                        wake: asyncio.Future[None] = (
                            asyncio.get_running_loop().create_future()
                        )
                        asyncio.get_running_loop().call_soon(wake.set_result, None)
                        await wake
                    process.release(deadline)
            finally:
                for process in processes:
                    process.dispose_observation()
                for anchor in anchors:
                    anchor.close()

        assert len(processes) == 2
        assert exited == [True, True]
        assert all(process.closed for process in processes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "staging",
    ["empty", "partial", "garbage", "symlink", "directory", "fifo", "hardlink", "mode"],
)
async def test_recovery_discards_only_verified_abandoned_journal_staging(
    tmp_path: Path, staging: str
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
    journal_path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    saved = journal_path.read_bytes()
    journal = JournalSnapshot.model_validate_json(saved)
    path = journal_path.with_name("next")
    target = tmp_path / "retained-target"
    target.write_bytes(b"preserved")
    target.chmod(0o600)
    eligible = staging in ("empty", "partial", "garbage")
    if staging == "symlink":
        path.symlink_to(target)
    elif staging == "directory":
        path.mkdir(mode=0o700)
    elif staging == "fifo":
        os.mkfifo(path, 0o600)
    elif staging == "hardlink":
        os.link(target, path)
    else:
        path.write_bytes(
            b"" if staging == "empty" else b"{" if staging == "partial" else b"invalid"
        )
        path.chmod(0o644 if staging == "mode" else 0o600)
    staged_identity = path.lstat()
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        limits=limits(),
        recovery=reference,
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )

    try:
        async with asyncio.timeout(5):
            if eligible:
                await owner.ready()
                recovered = JournalSnapshot.model_validate_json(
                    journal_path.read_bytes()
                )

                assert recovered.generation == journal.generation + 1
                assert recovered.invocation_id == journal.invocation_id
                assert recovered.entries == journal.entries
                assert not path.exists()
                assert (await owner.close(None)).state is CleanupOutcome.CLOSED
                assert not journal_path.parent.exists()
            else:
                with pytest.raises(LifetimeError):
                    await owner.ready()

                assert journal_path.read_bytes() == saved
                retained = path.lstat()
                assert (retained.st_dev, retained.st_ino, retained.st_mode) == (
                    staged_identity.st_dev,
                    staged_identity.st_ino,
                    staged_identity.st_mode,
                )
            assert target.read_bytes() == b"preserved"
    finally:
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["duplicate", "parent", "kind", "operation", "revision", "pending"]
)
async def test_corrupt_journal_cannot_lose_live_process_ownership(
    tmp_path: Path, corruption: str
) -> None:
    async with retained_live_invocation(tmp_path, "pair") as acquired:
        reference = acquired.reference
        path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
        journal = JournalSnapshot.model_validate_json(path.read_bytes())
        clock = SystemClock()
        processes = list(acquired.processes)
        try:
            assert len(processes) == 2
            original = journal.entries[-1]
            if corruption == "duplicate":
                changed = original.model_copy(
                    update={"resource_id": journal.entries[-2].resource_id}
                )
            elif corruption == "parent":
                changed = original.model_copy(
                    update={"parent_resource_id": original.resource_id}
                )
            elif corruption == "kind":
                changed = original.model_copy(
                    update={"kind": ResourceKind.TEMPORARY_TREE}
                )
            elif corruption == "operation":
                changed = original.model_copy(update={"acquisition_id": "e" * 32})
            elif corruption == "revision":
                changed = original.model_copy(update={"revision": journal.revision + 1})
            else:
                changed = original
            corrupt = journal.model_copy(
                update={
                    "entries": (*journal.entries[:-1], changed),
                    "pending_effects": ("f" * 32,)
                    if corruption == "pending"
                    else journal.pending_effects,
                }
            )
            retained = corrupt.model_dump_json().encode()
            path.write_bytes(retained)
            owner = ParentLifetime(
                limits=limits(),
                recovery=reference,
                launcher=InstalledIOHelperLauncher(
                    home=tmp_path, state_home=tmp_path / "state"
                ),
            )

            try:
                with pytest.raises(LifetimeError):
                    await owner.ready()

                assert path.read_bytes() == retained
                assert all(
                    process.observe(acquired.cleanup()).live_members
                    for process in processes
                )
            finally:
                await owner.close(None)
        finally:
            await reap_recovered(
                processes,
                clock,
                (SignalResult.SENT, SignalResult.SETTLED),
                acquired.cleanup,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["bootstrap_close", "substituted_tree"])
async def test_recovery_close_preserves_complete_inventory(
    tmp_path: Path, case: str
) -> None:
    async with retained_live_invocation(tmp_path, "live") as acquired:
        import asyncio
        import os

        from kora.protocols.lifetime import CleanupOutcome, TemporaryTreeRegistration
        from kora.runner.filesystem import DirectoryAnchor
        from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
        from tests.conformance.test_lifetime_io_helper import entered_fifo
        from tests.conformance.test_lifetime_pending_helpers import (
            ControlledLifetimeClock,
        )

        reference = acquired.reference
        path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
        journal = JournalSnapshot.model_validate_json(path.read_bytes())
        registration = journal.entries[-1].registration
        assert isinstance(registration, ProcessRegistration)
        independent_clock = SystemClock()
        independent = acquired.processes[0]
        try:
            loop = asyncio.get_running_loop()
            barrier = DirectoryAnchor.open_path(tmp_path)
            os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
            reading, writing = os.pipe()
            launcher = ProbeIOHelperLauncher(
                tmp_path, writing, barrier.descriptor, pause_at=1
            )
            clock = ControlledLifetimeClock()
            owner = ParentLifetime(
                limits=limits(), recovery=reference, launcher=launcher, clock=clock
            )
            try:
                await launcher.wait_for_start(1)
                with await entered_fifo(
                    barrier.descriptor,
                    reading,
                    independent_clock.monotonic_ns() + 5_000_000_000,
                    independent_clock,
                ) as writer:
                    if case == "substituted_tree":
                        tree = journal.entries[0].registration
                        assert isinstance(tree, TemporaryTreeRegistration)
                        tree_path = path.parent / tree.name
                        tree_path.rename(path.parent / "retained-original")
                        tree_path.mkdir(mode=0o700)
                        closing = None
                    else:
                        closing = asyncio.create_task(owner.close(None))
                        await owner.closure_started.wait()

                    writer.close()
                    if case == "substituted_tree":
                        with pytest.raises(LifetimeError):
                            await owner.ready()
                        retained = path.read_bytes()
                        closing = asyncio.create_task(owner.close(None))
                        await owner.closure_started.wait()
                        clock.advance(
                            int(owner.limits.cooperative_seconds * 1_000_000_000) + 1
                        )
                        reap_deadline = acquired.cleanup()
                        while not independent.observe(reap_deadline).settled:
                            assert independent_clock.monotonic_ns() < reap_deadline
                            wake: asyncio.Future[None] = loop.create_future()
                            loop.call_soon(wake.set_result, None)
                            await wake
                        clock.advance(
                            int(owner.limits.term_seconds * 1_000_000_000) + 1
                        )
                        report = await asyncio.wait_for(closing, 5)
                        assert report.state is CleanupOutcome.UNKNOWN
                        assert path.read_bytes() == retained
                        assert tuple(owner.records) == tuple(
                            entry.resource_id for entry in journal.entries
                        )
                    else:
                        assert closing is not None
                        await owner.ready()
                        clock.advance(
                            int(owner.limits.cooperative_seconds * 1_000_000_000) + 1
                        )
                        reap_deadline = acquired.cleanup()
                        while not independent.observe(reap_deadline).settled:
                            assert independent_clock.monotonic_ns() < reap_deadline
                            wake = loop.create_future()
                            loop.call_soon(wake.set_result, None)
                            await wake
                        clock.advance(
                            int(owner.limits.term_seconds * 1_000_000_000) + 1
                        )
                        report = await asyncio.wait_for(closing, 5)
                        assert report.state is CleanupOutcome.CLOSED
                        assert independent.observe(acquired.cleanup()).settled
                        assert not path.parent.exists()
            finally:
                try:
                    await dispose_test_owner(owner, clock, acquired.cleanup)
                finally:
                    os.close(reading)
                    os.close(writing)
                    barrier.close()
        finally:
            await reap_recovered(
                (independent,),
                independent_clock,
                (SignalResult.SENT, SignalResult.SETTLED),
                acquired.cleanup,
            )


class ExpiringIdentityReader:
    def __init__(self) -> None:
        from tests.conformance.test_lifetime_pending_helpers import (
            ControlledLifetimeClock,
        )

        self.clock = ControlledLifetimeClock()
        self.native = NativeIdentityReader(clock=self.clock)
        self.expiry: int | None = None
        self.after_expiry_reads = 0

    def read(self, pid: int) -> IdentityObservation:
        if self.expiry is not None and self.clock.monotonic_ns() >= self.expiry:
            self.after_expiry_reads += 1
        return self.native.read(pid)

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        if self.expiry is None:
            return self.native.group(process_group_id, deadline_ns)
        self.clock.advance(max(0, self.expiry - self.clock.monotonic_ns()))
        return GroupObservation(IdentityState.UNKNOWN)


@pytest.mark.asyncio
async def test_close_report_never_restarts_expired_process_observation(
    tmp_path: Path,
) -> None:
    async with retained_live_invocation(tmp_path, "live") as acquired:
        from kora.protocols.lifetime import CleanupOutcome

        reference = acquired.reference
        path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
        journal = JournalSnapshot.model_validate_json(path.read_bytes())
        registration = journal.entries[-1].registration
        assert isinstance(registration, ProcessRegistration)
        independent_clock = SystemClock()
        independent = acquired.processes[0]
        try:
            reader = ExpiringIdentityReader()
            owner = ParentLifetime(
                limits=limits(),
                recovery=reference,
                identities=reader,
                clock=reader.clock,
                launcher=InstalledIOHelperLauncher(
                    home=tmp_path, state_home=tmp_path / "state"
                ),
            )
            await owner.ready()
            reader.expiry = reader.clock.monotonic_ns() + int(
                (
                    owner.limits.cooperative_seconds
                    + owner.limits.term_seconds
                    + owner.limits.kill_observation_seconds
                    + owner.limits.drain_seconds
                )
                * 1_000_000_000
            )

            report = await owner.close(None)

            assert report.state is CleanupOutcome.UNKNOWN
            assert reader.after_expiry_reads == 0
            assert path.exists()
        finally:
            await reap_recovered(
                (independent,),
                independent_clock,
                (SignalResult.SENT,),
                acquired.cleanup,
            )


@pytest.mark.asyncio
async def test_recovery_control_capacity_keeps_entire_older_inventory(
    tmp_path: Path,
) -> None:
    async with retained_live_invocation(tmp_path, "live") as acquired:
        from kora.conformance.journal import JournalEntry
        from kora.protocols.lifetime import (
            CleanupOutcome,
            ResourceState,
            new_operation_id,
        )

        reference = acquired.reference
        path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
        original = JournalSnapshot.model_validate_json(path.read_bytes())
        registration = original.entries[-1].registration
        assert isinstance(registration, ProcessRegistration)
        clock = SystemClock()
        independent = acquired.processes[0]
        try:
            extended = original.model_copy(
                update={
                    "entries": (
                        *original.entries,
                        *(
                            JournalEntry(
                                resource_id=new_operation_id(),
                                parent_resource_id=None,
                                kind=ResourceKind.TEMPORARY_TREE,
                                state=ResourceState.RESERVED,
                                revision=1,
                            )
                            for _ in range(100)
                        ),
                    )
                }
            )
            path.write_bytes(extended.model_dump_json().encode())
            owner = ParentLifetime(
                limits=limits(),
                recovery=reference,
                launcher=InstalledIOHelperLauncher(
                    home=tmp_path, state_home=tmp_path / "state"
                ),
            )

            with pytest.raises(LifetimeError):
                await owner.ready()
            after_takeover = path.read_bytes()
            report = await owner.close(None)

            assert report.state is CleanupOutcome.UNKNOWN
            assert report.unresolved_count == len(extended.entries)
            assert len(owner.records) == len(extended.entries)
            assert {entry.resource_id for entry in extended.entries} == set(
                owner.records
            )
            assert (
                owner.records[original.entries[-1].resource_id].entry.registration
                == registration
            )
            assert path.read_bytes() == after_takeover
            assert (
                JournalSnapshot.model_validate_json(after_takeover).entries
                == extended.entries
            )
            assert owner.dispose_recovery_observers()
        finally:
            await reap_recovered(
                (independent,), clock, (SignalResult.SENT,), acquired.cleanup
            )


@pytest.mark.asyncio
async def test_live_probe_corrupt_journal_releases_acquired_processes(
    tmp_path: Path,
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))

    async with retained_live_invocation(tmp_path, "pair") as acquired:
        path = journal_file(tmp_path, acquired.reference.invocation_id)
        path.write_bytes(b"invalid")
        with pytest.raises(ValidationError):
            JournalSnapshot.model_validate_json(path.read_bytes())
    gc.collect()

    assert path.read_bytes() == b"invalid"
    assert_recovered_processes_absent(acquired.processes)
    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["before_report", "partial", "refused", "cancel_before_ack", "ack_failure"]
)
async def test_live_probe_handoff_failure_releases_acquired_processes(
    tmp_path: Path, phase: str
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    entered = asyncio.Event()
    started: list[subprocess.Popen[bytes]] = []
    diagnostics: list[str] = []
    observed: list[BaseException] = []

    async def acquire() -> None:
        async with retained_live_invocation(
            tmp_path,
            "pair",
            phase=phase,
            entered=entered,
            started=started,
            diagnostics=diagnostics,
        ):
            raise AssertionError("unexpected successful handoff")

    task = asyncio.create_task(acquire())
    try:
        if phase in ("partial", "cancel_before_ack"):
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif phase == "before_report":
            with pytest.raises(ChannelError) as expired:
                await task
            observed.append(expired.value)
            assert expired.value.reason is ChannelFailure.DEADLINE
        elif phase == "refused":
            with pytest.raises(ValidationError) as refused:
                await task
            observed.append(refused.value)
        else:
            with pytest.raises(ValueError, match="closed") as failed:
                await task
            observed.append(failed.value)
    finally:
        await join_helper_operation(task, observed)
    gc.collect()

    assert len(started) == 1
    assert started[0].returncode == 0, diagnostics
    assert NativeIdentityReader().read(started[0].pid).state is IdentityState.ABSENT
    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_ack", "after_ack"])
async def test_live_probe_repeated_cancellation_joins_owned_cleanup(
    tmp_path: Path, when: str
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    started: list[subprocess.Popen[bytes]] = []
    diagnostics: list[str] = []
    observers: list[RecoveredProcess] = []

    async def acquire() -> None:
        async with retained_live_invocation(
            tmp_path,
            "pair",
            phase="cancel_before_ack" if when == "before_ack" else "complete",
            entered=entered,
            started=started,
            cleanup_entered=cleanup_entered,
            cleanup_release=cleanup_release,
            diagnostics=diagnostics,
        ) as acquired:
            observers.extend(acquired.processes)
            entered.set()
            waiting: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            await waiting

    task = asyncio.create_task(acquire())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("original acquisition")
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        assert not task.done()
        if when == "before_ack":
            assert started[0].poll() is None
        else:
            assert len(observers) == 2
            for observer in observers:
                native = NativeIdentityReader().read(observer.registration.process.pid)
                assert native.state is IdentityState.PRESENT
                assert native.process is not None
                assert not native.process.dead
        task.cancel("later teardown")
        await turn()
        assert not task.done()
        task.cancel("last teardown")
        await turn()
        assert not task.done()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert cancelled.value.args == ("original acquisition",)
    finally:
        cleanup_release.set()
        await join_helper_operation(task)
    gc.collect()

    assert started[0].returncode == 0, diagnostics
    assert NativeIdentityReader().read(started[0].pid).state is IdentityState.ABSENT
    assert_recovered_processes_absent(observers)
    assert set(os.listdir("/dev/fd")) == baseline
