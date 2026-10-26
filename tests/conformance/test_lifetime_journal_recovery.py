import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

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
from kora.runner.identity import (
    GroupObservation,
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
)
from kora.runner.processes import RecoveredProcess, SignalResult
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


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
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "pair",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    journal = JournalSnapshot.model_validate_json(path.read_bytes())
    clock = SystemClock()
    processes = [
        RecoveredProcess(entry.registration, clock, NativeIdentityReader())
        for entry in journal.entries
        if isinstance(entry.registration, ProcessRegistration)
    ]
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
        changed = original.model_copy(update={"kind": ResourceKind.TEMPORARY_TREE})
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
            process.observe(clock.monotonic_ns() + 5_000_000_000).live_members
            for process in processes
        )
    finally:
        await owner.close(None)
        for process in processes:
            deadline = clock.monotonic_ns() + 5_000_000_000
            assert process.signal(signal.SIGKILL, deadline) in (
                SignalResult.SENT,
                SignalResult.SETTLED,
            )
            await process.settle(deadline)
            process.release(deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["bootstrap_close", "substituted_tree"])
async def test_recovery_close_preserves_complete_inventory(
    tmp_path: Path, case: str
) -> None:
    import asyncio
    import os

    from kora.protocols.lifetime import CleanupOutcome, TemporaryTreeRegistration
    from kora.runner.filesystem import DirectoryAnchor
    from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
    from tests.conformance.test_lifetime_io_helper import entered_fifo
    from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock

    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "live",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    journal = JournalSnapshot.model_validate_json(path.read_bytes())
    registration = journal.entries[-1].registration
    assert isinstance(registration, ProcessRegistration)
    independent_clock = SystemClock()
    independent = RecoveredProcess(
        registration, independent_clock, NativeIdentityReader()
    )
    barrier = DirectoryAnchor.open_path(tmp_path)
    os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
    reading, writing = os.pipe()
    launcher = ProbeIOHelperLauncher(tmp_path, writing, barrier.descriptor, pause_at=1)
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        limits=limits(), recovery=reference, launcher=launcher, clock=clock
    )
    loop = asyncio.get_running_loop()
    while not owner.mutations.helpers:
        wake: asyncio.Future[None] = loop.create_future()
        loop.call_soon(wake.set_result, None)
        await wake
    helper = owner.mutations.helpers[-1]
    await entered_fifo(helper.process.pid, reading, owner.deadline(), clock)
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

    try:
        writer = os.open(
            "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
        )
        os.close(writer)
        if case == "substituted_tree":
            with pytest.raises(LifetimeError):
                await owner.ready()
            retained = path.read_bytes()
            closing = asyncio.create_task(owner.close(None))
            await owner.closure_started.wait()
            clock.advance(int(owner.limits.cooperative_seconds * 1_000_000_000) + 1)
            reap_deadline = independent_clock.monotonic_ns() + 5_000_000_000
            while not independent.observe(reap_deadline).settled:
                assert independent_clock.monotonic_ns() < reap_deadline
                wake = loop.create_future()
                loop.call_soon(wake.set_result, None)
                await wake
            clock.advance(int(owner.limits.term_seconds * 1_000_000_000) + 1)
            report = await asyncio.wait_for(closing, 5)
            assert report.state is CleanupOutcome.UNKNOWN
            assert path.read_bytes() == retained
            assert tuple(owner.records) == tuple(
                entry.resource_id for entry in journal.entries
            )
        else:
            assert closing is not None
            await owner.ready()
            clock.advance(int(owner.limits.cooperative_seconds * 1_000_000_000) + 1)
            reap_deadline = independent_clock.monotonic_ns() + 5_000_000_000
            while not independent.observe(reap_deadline).settled:
                assert independent_clock.monotonic_ns() < reap_deadline
                wake = loop.create_future()
                loop.call_soon(wake.set_result, None)
                await wake
            clock.advance(int(owner.limits.term_seconds * 1_000_000_000) + 1)
            report = await asyncio.wait_for(closing, 5)
            assert report.state is CleanupOutcome.CLOSED
            assert independent.observe(
                independent_clock.monotonic_ns() + 5_000_000_000
            ).settled
            assert not path.parent.exists()
    finally:
        deadline = independent_clock.monotonic_ns() + 5_000_000_000
        assert independent.signal(signal.SIGKILL, deadline) in (
            SignalResult.SENT,
            SignalResult.SETTLED,
        )
        await independent.settle(deadline)
        independent.release(deadline)
        if not helper.closed:
            helper.signal(signal.SIGKILL)
            await helper.settle(deadline)
        os.close(reading)
        os.close(writing)
        barrier.close()


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
    from kora.protocols.lifetime import CleanupOutcome

    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "live",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    journal = JournalSnapshot.model_validate_json(path.read_bytes())
    registration = journal.entries[-1].registration
    assert isinstance(registration, ProcessRegistration)
    independent_clock = SystemClock()
    independent = RecoveredProcess(
        registration, independent_clock, NativeIdentityReader()
    )
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

    try:
        report = await owner.close(None)

        assert report.state is CleanupOutcome.UNKNOWN
        assert reader.after_expiry_reads == 0
        assert path.exists()
    finally:
        deadline = independent_clock.monotonic_ns() + 5_000_000_000
        assert independent.signal(signal.SIGKILL, deadline) is SignalResult.SENT
        await independent.settle(deadline)
        independent.release(deadline)


@pytest.mark.asyncio
async def test_recovery_control_capacity_keeps_entire_older_inventory(
    tmp_path: Path,
) -> None:
    from kora.conformance.journal import JournalEntry
    from kora.protocols.lifetime import CleanupOutcome, ResourceState, new_operation_id

    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "live",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    original = JournalSnapshot.model_validate_json(path.read_bytes())
    registration = original.entries[-1].registration
    assert isinstance(registration, ProcessRegistration)
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
    clock = SystemClock()
    independent = RecoveredProcess(registration, clock, NativeIdentityReader())
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
        after_takeover = path.read_bytes()
        report = await owner.close(None)

        assert report.state is CleanupOutcome.UNKNOWN
        assert report.unresolved_count == len(extended.entries)
        assert len(owner.records) == len(extended.entries)
        assert {entry.resource_id for entry in extended.entries} == set(owner.records)
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
        deadline = clock.monotonic_ns() + 5_000_000_000
        assert independent.signal(signal.SIGKILL, deadline) is SignalResult.SENT
        await independent.settle(deadline)
        independent.release(deadline)
