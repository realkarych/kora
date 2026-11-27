import asyncio
import errno
import fcntl
import inspect
import multiprocessing
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import cast

import pytest

from kora.db.custody_journal import (
    ACTIVE_LEASES,
    CustodyJournalTransaction,
    NativeRow,
    NativeValue,
    journal_lease,
    open_journal,
    recovery_registration_values,
)
from kora.db.custody_models import (
    CustodyParticipantRegistration,
    CustodyProcessIdentity,
    CustodyRecoveryRegistration,
    CustodyUnavailable,
)
from kora.db.custody_native import (
    DESCRIPTORS,
    FORK_GUARD,
    CustodyLatch,
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    CustodyNativeWait,
)
from kora.db.custody_recovery import CustodyRecoveryPriority
from kora.db.custody_schema import AUTHORITY, RECOVERY
from kora.sqlite.driver import Connection
from kora.standalone.custody import NativeCustodyPlatform
from tests.api.guarded_sqlite_prebind_child import (
    held_main_descriptors,
    native_identity,
)
from tests.db.closing import ConnectionLedger, sqlite_contender, until
from tests.db.test_custody_journal import authority, custody_database
from tests.db.test_custody_native import journal_path
from tests.support.databases import copy_sqlite_template
from tests.support.observability import run_python

JOURNAL_ID = b"j" * 16
GENERATION = 2
BARRIER_TIMEOUT = 12.0


class Barrier:
    def __init__(self) -> None:
        self.entered: threading.Event = threading.Event()
        self.release: threading.Event = threading.Event()

    def pause(self) -> None:
        self.entered.set()
        if not self.release.wait(BARRIER_TIMEOUT):
            raise TimeoutError("native test barrier was not released")


def platform_caller() -> str:
    frame = inspect.currentframe()
    for _ in range(3):
        if frame is None:
            raise RuntimeError("native test observer has no caller frame")
        frame = frame.f_back
    if frame is None:
        raise RuntimeError("native test observer has no caller frame")
    return frame.f_code.co_name


class ObservedPlatform(NativeCustodyPlatform):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path: Path = path
        self.before_commit: Barrier | None = None
        self.before_sync: tuple[CustodyLatch, Barrier] | None = None
        self.during_open_check: Barrier | None = None
        self.before_ordinary_return: Barrier | None = None
        self.ordinary_worker_visits: int = 0

    def current_process(self) -> CustodyProcessIdentity:
        identity = super().current_process()
        worker = threading.current_thread().name
        publication = self.before_commit
        ordinary = self.during_open_check
        if (
            publication is not None
            and worker.startswith("kora-custody-recovery")
            and (self.path / "authority.sqlite3-journal").exists()
        ):
            self.before_commit = None
            publication.pause()
        if (
            ordinary is not None
            and worker.startswith("late-ordinary")
            and platform_caller() == "_open_lock"
        ):
            self.during_open_check = None
            ordinary.pause()
        if worker.startswith("kora-custody_"):
            self.ordinary_worker_visits += 1
            returning = self.before_ordinary_return
            if returning is not None and platform_caller() == "_perform":
                self.before_ordinary_return = None
                returning.pause()
        return identity

    def persist_file(self, descriptor: int) -> None:
        awaiting = self.before_sync
        if awaiting is not None:
            latch, barrier = awaiting
            named = (self.path / "recovery.lock").stat()
            handle = os.fstat(descriptor)
            if (named.st_dev, named.st_ino) == (
                handle.st_dev,
                handle.st_ino,
            ) and os.pread(descriptor, 2, 0) == latch.value:
                self.before_sync = None
                barrier.pause()
        super().persist_file(descriptor)


async def initialize(root: CustodyNativeDirectory) -> None:
    async with journal_lease(root) as journal:
        await journal.initialize()
        await journal.run(lambda tx: tx.insert(AUTHORITY, authority(root, 12)))


async def register(priority: CustodyRecoveryPriority) -> int:
    return (
        await priority.register(journal_id=JOURNAL_ID, generation=GENERATION)
    ).incarnation


async def ordinary_registration(
    root: CustodyNativeDirectory, lifetime: CustodyLifetimeLease
) -> CustodyParticipantRegistration:
    async with journal_lease(root, ordinary=(lifetime, None)) as lease:
        return await lease.run(
            lambda tx: tx.reserve_ordinary_participant(
                lifetime, journal_id=JOURNAL_ID, generation=GENERATION
            )
        )


def descriptor_count(path: Path) -> int:
    target = path.stat()
    with FORK_GUARD:
        descriptors = tuple(DESCRIPTORS)
        observed = [os.fstat(owned.descriptor) for owned in descriptors]
    return sum(
        (handle.st_dev, handle.st_ino) == (target.st_dev, target.st_ino)
        for handle in observed
    )


def read_authority(path: Path) -> NativeRow:
    with sqlite_contender(path / "authority.sqlite3", readonly=True) as connection:
        with closing(connection.execute("SELECT * FROM authority")) as cursor:
            row = cast("tuple[NativeValue, ...] | None", cursor.fetchone())
            assert row is not None
            assert cursor.description is not None
            return dict(
                zip((column[0] for column in cursor.description), row, strict=True)
            )


def expect(signal: socket.socket, value: bytes) -> None:
    assert signal.recv(1) == value


async def receive(signal: socket.socket, value: bytes) -> None:
    await asyncio.to_thread(expect, signal, value)


async def incumbent_worker(root: CustodyNativeDirectory, signal: socket.socket) -> None:
    lifetime = CustodyLifetimeLease(root)
    try:
        participant = await ordinary_registration(root, lifetime)
        async with journal_lease(root, ordinary=(lifetime, participant)) as journal:
            signal.sendall(b"C")
            await receive(signal, b"J")
            phase = Barrier()

            def hold(tx: CustodyJournalTransaction) -> NativeRow | None:
                phase.pause()
                return tx.read(AUTHORITY)

            order_path = root.path / "journal-order.lock"
            baseline = descriptor_count(order_path)
            work = asyncio.create_task(journal.run(hold))
            try:
                await until(
                    lambda: descriptor_count(order_path) > baseline or work.done()
                )
                signal.sendall(b"S")
                await until(lambda: phase.entered.is_set() or work.done())
                assert phase.entered.is_set()
                signal.sendall(b"J")
                await receive(signal, b"F")
            finally:
                phase.release.set()
                assert await work is not None
            signal.sendall(b"F")
            await receive(signal, b"X")
        signal.sendall(b"X")
    finally:
        lifetime.close()


async def remote_incumbent(path: Path, descriptor: int) -> None:
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
    try:
        with socket.socket(fileno=descriptor) as signal:
            signal.settimeout(BARRIER_TIMEOUT)
            await incumbent_worker(root, signal)
    finally:
        root.close()


@asynccontextmanager
async def remote_actor(path: Path, operation: str) -> AsyncGenerator[socket.socket]:
    control, actor = socket.socketpair()
    with (
        control,
        actor,
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import asyncio,sys; from pathlib import Path; "
                "from tests.db.test_custody_recovery_priority "
                "import remote_incumbent, remote_latch_observer; " + operation,
                str(path),
                str(actor.fileno()),
            ],
            pass_fds=(actor.fileno(),),
        ) as process,
    ):
        control.settimeout(BARRIER_TIMEOUT)
        actor.close()
        try:
            yield control
            assert await asyncio.to_thread(process.wait, BARRIER_TIMEOUT) == 0
        finally:
            if process.poll() is None:
                process.kill()


@asynccontextmanager
async def incumbent(
    root: CustodyNativeDirectory, *, separate_process: bool
) -> AsyncGenerator[socket.socket]:
    if separate_process:
        async with remote_actor(
            root.path,
            "asyncio.run(remote_incumbent(Path(sys.argv[1]),int(sys.argv[2])))",
        ) as control:
            await receive(control, b"C")
            yield control
        return
    control, actor = socket.socketpair()
    with control, actor:
        control.settimeout(BARRIER_TIMEOUT)
        actor.settimeout(BARRIER_TIMEOUT)
        task = asyncio.create_task(incumbent_worker(root, actor))
        try:
            await receive(control, b"C")
            yield control
            await asyncio.wait_for(task, BARRIER_TIMEOUT)
        finally:
            if not task.done():
                control.close()
                await task


async def start_phase(control: socket.socket) -> None:
    control.sendall(b"J")
    await receive(control, b"S")


async def finish_incumbent(control: socket.socket) -> None:
    control.sendall(b"F")
    await receive(control, b"F")
    control.sendall(b"X")
    await receive(control, b"X")


async def refuse_before_allocation(
    root: CustodyNativeDirectory,
    lifetime: CustodyLifetimeLease,
    participant: CustodyParticipantRegistration,
) -> None:
    active = set(ACTIVE_LEASES)
    with pytest.raises(CustodyUnavailable):
        async with journal_lease(root, ordinary=(lifetime, participant)):
            pytest.fail("a fresh ordinary owner passed denial")
    with pytest.raises(CustodyUnavailable):
        await ordinary_registration(root, lifetime)
    assert set(ACTIVE_LEASES) == active


async def remote_latch_observer(path: Path, descriptor: int) -> None:
    with (
        socket.socket(fileno=descriptor) as signal,
        closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
        ) as root,
        closing(CustodyLifetimeLease(root)) as lifetime,
    ):
        signal.settimeout(BARRIER_TIMEOUT)
        participant = await ordinary_registration(root, lifetime)
        async with journal_lease(root, ordinary=(lifetime, participant)):
            assert root.read_latch() is CustodyLatch.OPEN
        signal.sendall(b"R")
        await receive(signal, b"D")
        await refuse_before_allocation(root, lifetime, participant)
        with closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
        ) as fresh:
            assert fresh.read_latch() is CustodyLatch.ARMED
            with pytest.raises(CustodyUnavailable):
                fresh.require_open_latch()
        signal.sendall(b"D")


async def assert_bounded_cancellations(request: asyncio.Task[None]) -> None:
    existing = asyncio.all_tasks()
    for _ in range(8):
        assert request.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not request.done()
        assert asyncio.all_tasks() <= existing


@pytest.mark.asyncio
async def test_recovery_priority_blocks_new_checkout_and_preserves_existing_owner(
    tmp_path: Path, sqlite_template: Path
) -> None:
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    priority = CustodyRecoveryPriority(root)
    lifetime = CustodyLifetimeLease(root)
    settings = copy_sqlite_template(sqlite_template, tmp_path / "custody.db")
    database = custody_database(settings)
    ledger = ConnectionLedger(database)
    try:
        await initialize(root)
        await register(priority)
        participant = await ordinary_registration(root, lifetime)
        ordinary = (lifetime, participant)
        commit_path = path / "commit.lock"
        async with journal_lease(root, ordinary=ordinary):
            baseline = descriptor_count(commit_path)

            async with database.writer_engine.connect():
                request = asyncio.create_task(priority.request())
                await until(lambda: descriptor_count(commit_path) > baseline)
                await refuse_before_allocation(root, lifetime, participant)
                assert not request.done()
                assert ledger.outstanding == ledger.checkouts == 1
        await request
        await priority.withdraw()
        async with journal_lease(root, ordinary=ordinary):
            async with database.writer_engine.connect():
                assert ledger.outstanding == 1
    finally:
        await database.close()
        lifetime.close()
        priority.close()
        root.close()

    assert ledger.checkouts == 2
    assert ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_process", [False, True], ids=["local", "process"])
async def test_custody_priority_survives_ordinary_journal_transaction(
    tmp_path: Path, separate_process: bool
) -> None:
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    priority = CustodyRecoveryPriority(root)
    lifetime = CustodyLifetimeLease(root)
    try:
        await initialize(root)
        incarnation = await register(priority)
        participant = await ordinary_registration(root, lifetime)
        async with incumbent(root, separate_process=separate_process) as control:
            before = read_authority(path)
            await start_phase(control)
            await receive(control, b"J")
            order_path = path / "journal-order.lock"
            descriptors = descriptor_count(order_path)

            request = asyncio.create_task(priority.request())
            await until(lambda: descriptor_count(order_path) > descriptors)
            await refuse_before_allocation(root, lifetime, participant)
            await assert_bounded_cancellations(request)
            await finish_incumbent(control)
            with pytest.raises(asyncio.CancelledError):
                await request
            published = read_authority(path)
            await priority.withdraw()
            withdrawn = read_authority(path)
    finally:
        lifetime.close()
        priority.close()
        root.close()

    assert published == {**before, "recovery_priority_incarnation": incarnation}
    assert withdrawn == before


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_process", [False, True], ids=["local", "process"])
async def test_custody_priority_preserves_incumbent_journal_phase(
    tmp_path: Path, separate_process: bool
) -> None:
    path = journal_path(tmp_path)
    platform = ObservedPlatform(path)
    root = CustodyNativeDirectory(path, platform, provision=True)
    priority = CustodyRecoveryPriority(root)
    try:
        await initialize(root)
        incarnation = await register(priority)
        publication = Barrier()
        async with incumbent(root, separate_process=separate_process) as control:
            platform.before_commit = publication
            request = asyncio.create_task(priority.request())
            try:
                await until(publication.entered.is_set)
                await start_phase(control)
                with pytest.raises(CustodyUnavailable):
                    await priority.request()
                with pytest.raises(CustodyUnavailable):
                    priority.close()
                with pytest.raises(BlockingIOError), root.acquire("journal-order.lock"):
                    pytest.fail("SQLite is still open in the publisher")
                assert not request.done()
            finally:
                publication.release.set()
            await receive(control, b"J")
            assert not request.done()
            await finish_incumbent(control)
            await request
            published = read_authority(path)
            await priority.withdraw()
    finally:
        priority.close()
        root.close()

    assert published["recovery_priority_incarnation"] == incarnation


class EarlierForkObserver:
    live: Connection | None = None
    inspected: int = 0
    child_fenced: bool = False

    def observe_parent(self) -> None:
        if self.live is not None:
            self.inspected += int(not self.live.settled())

    def observe_child(self) -> None:
        if self.live is not None:
            try:
                self.live.settled()
            except sqlite3.ProgrammingError:
                self.child_fenced = True


def check_fork_child(
    inherited: dict[int, os.stat_result],
    priority: CustodyRecoveryPriority,
    live: Connection,
    earlier: EarlierForkObserver,
) -> None:
    assert earlier.child_fenced
    assert not DESCRIPTORS
    for descriptor, previous in inherited.items():
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            assert (current.st_dev, current.st_ino) != (
                previous.st_dev,
                previous.st_ino,
            )
    with pytest.raises(CustodyUnavailable):
        priority.close()
    with pytest.raises(sqlite3.Error):
        live.execute("SELECT 1").close()


@contextmanager
def native_fork_receipts(
    root: CustodyNativeDirectory,
) -> Generator[tuple[Connection, tuple[int, ...]]]:
    main = root.path / "authority.sqlite3"
    identity = native_identity(main)
    with root.acquire("journal-order.lock"), closing(Connection()) as live:
        open_journal(root, live, initialize=False)
        live.execute("BEGIN IMMEDIATE").close()
        (first,) = held_main_descriptors(identity)
        with closing(Connection()) as refused:
            with pytest.raises(sqlite3.Error):
                refused.open(
                    main,
                    False,
                    0,
                    lambda fd, empty: False,
                    journal=root.admit_rollback_journal,
                    directory=root.journal_directory(),
                    custody=True,
                    authority_journal=True,
                )
            assert refused.acquired_main()
            receipts = held_main_descriptors(identity)
            assert len(receipts) == 2
            assert first in receipts
            assert not refused.settled()
            yield live, receipts
            assert held_main_descriptors(identity) == receipts
            assert live.in_transaction
            assert not refused.settled()
        live.rollback()
    assert refused.settled()
    assert not held_main_descriptors(identity)


def verify_fork_closed_copies(
    live: Connection,
    receipts: tuple[int, ...],
    priority: CustodyRecoveryPriority,
    earlier: EarlierForkObserver,
) -> None:
    with FORK_GUARD:
        inherited = {
            descriptor: os.fstat(descriptor)
            for descriptor in (
                *receipts,
                *(handle.descriptor for handle in DESCRIPTORS),
            )
        }
    child = multiprocessing.get_context("fork").Process(
        target=check_fork_child, args=(inherited, priority, live, earlier)
    )
    earlier.live = live
    child.start()
    try:
        child.join(BARRIER_TIMEOUT)
        assert child.exitcode == 0
        assert earlier.inspected == 2
    finally:
        earlier.live = None
        if child.is_alive():
            child.kill()
            child.join(BARRIER_TIMEOUT)
        child.close()


class NativeWaitObserver:
    def __init__(self) -> None:
        self.waiting: bool = False

    def profile(self, frame: FrameType, event: str, operation: object) -> None:
        if (
            threading.current_thread().name.startswith("kora-custody-recovery")
            and frame.f_code is CustodyNativeWait.acquire.__code__
            and operation is fcntl.flock
        ):
            self.waiting = event == "c_call"


async def run_fork_during_wait(path: Path) -> None:
    observer = NativeWaitObserver()
    saved_profile = threading.getprofile()
    threading.setprofile(observer.profile)
    try:
        await exercise_fork_during_wait(path, observer)
    finally:
        threading.setprofile(saved_profile)


async def exercise_fork_during_wait(path: Path, observer: NativeWaitObserver) -> None:
    earlier = EarlierForkObserver()
    os.register_at_fork(
        before=earlier.observe_parent,
        after_in_parent=earlier.observe_parent,
        after_in_child=earlier.observe_child,
    )
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    priority = CustodyRecoveryPriority(root)
    try:
        await initialize(root)
        await register(priority)
        async with incumbent(root, separate_process=False) as control:
            commit_path = path / "commit.lock"
            baseline = descriptor_count(commit_path)
            request = asyncio.create_task(priority.request())
            await until(
                lambda: descriptor_count(commit_path) > baseline and observer.waiting
            )
            request.cancel()
            with pytest.raises(CustodyUnavailable):
                priority.close()
            with closing(
                CustodyNativeDirectory(
                    path.with_name("native-fork"),
                    NativeCustodyPlatform(),
                    provision=True,
                )
            ) as native_root:
                await initialize(native_root)
                with native_fork_receipts(native_root) as (live, receipts):
                    verify_fork_closed_copies(live, receipts, priority, earlier)
            with pytest.raises(BlockingIOError), root.acquire("commit.lock"):
                pytest.fail("fork stole the parent's ownership")
            await start_phase(control)
            await receive(control, b"J")
            await finish_incumbent(control)
            with pytest.raises(asyncio.CancelledError):
                await request
            with pytest.raises(BlockingIOError), root.acquire("commit.lock"):
                pytest.fail("cancellation lost the original acquired descriptor")
            await priority.withdraw()
    finally:
        priority.close()
        root.close()
    assert not DESCRIPTORS


def test_custody_priority_waits_for_commit_owner_through_fork(tmp_path: Path) -> None:
    path = journal_path(tmp_path)

    result = run_python(
        [
            "-c",
            "import asyncio,sys; from pathlib import Path; "
            "from tests.db.test_custody_recovery_priority import run_fork_during_wait; "
            "asyncio.run(run_fork_during_wait(Path(sys.argv[1])))",
            str(path),
        ],
        {},
    )

    assert result.code == 0, result.stderr.decode()


async def late_ordinary(
    root: CustodyNativeDirectory,
    lifetime: CustodyLifetimeLease,
    participant: CustodyParticipantRegistration,
) -> None:
    with pytest.raises(CustodyUnavailable):
        async with journal_lease(root, ordinary=(lifetime, participant)):
            pytest.fail("the second OPEN read admitted a delayed writer")


@pytest.mark.asyncio
async def test_custody_priority_reconciles_latch_and_hint(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    platform = ObservedPlatform(path)
    root = CustodyNativeDirectory(path, platform, provision=True)
    priority = CustodyRecoveryPriority(root)
    lifetime = CustodyLifetimeLease(root)
    try:
        await initialize(root)
        await register(priority)
        participant = await ordinary_registration(root, lifetime)
        serving = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
        async with remote_actor(
            root.path,
            "asyncio.run(remote_latch_observer(Path(sys.argv[1]),int(sys.argv[2])))",
        ) as peer:
            await receive(peer, b"R")
            await check_live_latch(
                root, platform, priority, lifetime, participant, serving, peer
            )
    finally:
        lifetime.close()
        priority.close()
        root.close()


async def check_live_latch(
    root: CustodyNativeDirectory,
    platform: ObservedPlatform,
    priority: CustodyRecoveryPriority,
    lifetime: CustodyLifetimeLease,
    participant: CustodyParticipantRegistration,
    serving: CustodyNativeDirectory,
    peer: socket.socket,
) -> None:
    path = root.path
    with (
        closing(serving),
        closing(CustodyLifetimeLease(serving)) as serving_lifetime,
        ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="late-ordinary"
        ) as executor,
    ):
        async with journal_lease(serving, ordinary=(serving_lifetime, participant)):
            assert serving.read_latch() is CustodyLatch.OPEN
        late = Barrier()
        platform.during_open_check = late
        delayed = asyncio.get_running_loop().run_in_executor(
            executor,
            lambda: asyncio.run(late_ordinary(root, lifetime, participant)),
        )
        deny = Barrier()
        await until(late.entered.is_set)
        platform.before_sync = (CustodyLatch.ARMED, deny)
        request = asyncio.create_task(priority.request())
        try:
            await until(deny.entered.is_set)
            with closing(
                CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
            ) as fresh:
                assert fresh.read_latch() is CustodyLatch.ARMED
                with pytest.raises(CustodyUnavailable):
                    fresh.require_open_latch()
            await refuse_before_allocation(serving, serving_lifetime, participant)
            peer.sendall(b"D")
            await receive(peer, b"D")
            visits = platform.ordinary_worker_visits
            active = set(ACTIVE_LEASES)
            late.release.set()
            await delayed
            assert set(ACTIVE_LEASES) == active
            assert platform.ordinary_worker_visits == visits
            assert read_authority(path)["recovery_priority_incarnation"] is None
            with root.acquire("commit.lock"), root.acquire("journal-order.lock"):
                assert not request.done()
        finally:
            late.release.set()
            deny.release.set()
        await request
        opening = Barrier()
        platform.before_sync = (CustodyLatch.OPEN, opening)
        withdrawal = asyncio.create_task(priority.withdraw())
        try:
            await until(opening.entered.is_set)
            assert serving.read_latch() is CustodyLatch.OPEN
            assert read_authority(path)["recovery_priority_incarnation"] is None
            with root.acquire("commit.lock"), root.acquire("journal-order.lock"):
                assert not withdrawal.done()
            with pytest.raises(BlockingIOError), root.acquire("recovery.lock"):
                pytest.fail("the reopening owner released R before sync")
        finally:
            opening.release.set()
        await withdrawal
        async with journal_lease(root, ordinary=(lifetime, participant)):
            reopened = root.read_latch()
    assert reopened is CustodyLatch.OPEN


@pytest.mark.asyncio
async def test_recovery_registration_and_withdrawal_release_preopen_failure(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    priority = CustodyRecoveryPriority(root)
    sidecar = path / "authority.sqlite3-wal"
    try:
        await initialize(root)
        sidecar.touch(mode=0o600)
        with pytest.raises(CustodyUnavailable):
            await register(priority)
        with root.acquire("commit.lock"), root.acquire("journal-order.lock"):
            assert root.read_latch() is CustodyLatch.ARMED
        sidecar.unlink()
        incarnation = await register(priority)
        await priority.request()
        sidecar.touch(mode=0o600)
        with pytest.raises(CustodyUnavailable):
            await priority.withdraw()
        with root.acquire("journal-order.lock"):
            assert read_authority(path)["recovery_priority_incarnation"] == incarnation
        with pytest.raises(BlockingIOError), root.acquire("commit.lock"):
            pytest.fail("withdrawal lost the original C")
        sidecar.unlink()
        await priority.withdraw()
        final = read_authority(path)
    finally:
        priority.close()
        root.close()

    assert final["recovery_priority_incarnation"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged", [b"", b"\0", b"\xa5\xa5", b"\xff", b"\x3c"])
async def test_fresh_and_serving_peer_refuse_invalid_or_bootstrap_latch(
    tmp_path: Path, damaged: bytes
) -> None:
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    latch = path / "recovery.lock"
    try:
        await initialize(root)
        before = read_authority(path)
        latch.write_bytes(damaged)

        with pytest.raises(CustodyUnavailable):
            root.require_open_latch()
        if damaged == CustodyLatch.BOOTSTRAP_FROM_OPEN.value:
            with closing(
                CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
            ) as successor:
                with closing(CustodyRecoveryPriority(successor)) as priority:
                    with pytest.raises(CustodyUnavailable):
                        await register(priority)
        else:
            with pytest.raises(CustodyUnavailable):
                CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
        after = read_authority(path)
    finally:
        root.close()

    assert latch.read_bytes() == damaged
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("foreign_host", "incarnation"),
    [(False, 1), (True, 1), (False, 13)],
    ids=["prior-registration", "foreign-host", "unallocated"],
)
async def test_attached_coordinator_cannot_clear_surviving_hint(
    tmp_path: Path, foreign_host: bool, incarnation: int
) -> None:
    path = journal_path(tmp_path)
    with closing(
        CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    ) as root:
        previous = replace(
            root.process,
            host_id="foreign-host" if foreign_host else root.process.host_id,
            start_time=0 if root.process.start_time else 1,
        )
        saved = {**authority(root, 12), "recovery_priority_incarnation": incarnation}
        saved_recovery = recovery_registration_values(
            CustodyRecoveryRegistration(JOURNAL_ID, GENERATION, incarnation, previous)
        )
        async with journal_lease(root) as journal:
            await journal.initialize()
            await journal.run(lambda tx: tx.insert(AUTHORITY, saved))
            await journal.run(lambda tx: tx.insert(RECOVERY, saved_recovery))

    with closing(
        CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
    ) as attached:
        with closing(CustodyRecoveryPriority(attached)) as successor:
            with pytest.raises(CustodyUnavailable):
                await register(successor)
        async with journal_lease(attached) as journal:
            retained = await journal.run(lambda tx: tx.read(AUTHORITY))
            recovery = await journal.run(lambda tx: tx.read(RECOVERY))

    assert saved == retained
    assert recovery == saved_recovery
    assert (path / "recovery.lock").read_bytes() == CustodyLatch.ARMED.value


@pytest.mark.asyncio
async def test_ordinary_gate_rechecks_latch_after_journal_eligibility(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)
    platform = ObservedPlatform(path)
    with closing(CustodyNativeDirectory(path, platform, provision=True)) as root:
        with (
            closing(CustodyRecoveryPriority(root)) as priority,
            closing(CustodyLifetimeLease(root)) as lifetime,
        ):
            await initialize(root)
            await register(priority)
            participant = await ordinary_registration(root, lifetime)
            eligibility = Barrier()
            platform.before_ordinary_return = eligibility
            ordinary = asyncio.create_task(late_ordinary(root, lifetime, participant))

            request: asyncio.Task[None] | None = None
            try:
                await until(eligibility.entered.is_set)
                request = asyncio.create_task(priority.request())
                await until(lambda: root.read_latch() is CustodyLatch.ARMED)
                assert not request.done()
            finally:
                eligibility.release.set()
                await ordinary
            assert request is not None
            await request
            await priority.withdraw()

    assert (path / "recovery.lock").read_bytes() == CustodyLatch.OPEN.value


@pytest.mark.asyncio
async def test_recovery_admission_and_close_refuse_another_loop(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    with closing(
        CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    ) as root:
        with closing(CustodyRecoveryPriority(root)) as priority:
            await initialize(root)
            await register(priority)

            with pytest.raises(CustodyUnavailable):
                await asyncio.to_thread(lambda: asyncio.run(priority.request()))
            with pytest.raises(CustodyUnavailable):
                await asyncio.to_thread(priority.close)
            unchanged = root.read_latch()
            await priority.request()
            await priority.withdraw()

    assert unchanged is CustodyLatch.OPEN


@pytest.mark.asyncio
async def test_retained_open_from_previous_boot_refuses_ordinary_registration(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)
    with closing(
        CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    ) as root:
        await initialize(root)
        other_boot = (
            "previous-boot" if root.process.boot_id != "previous-boot" else "older-boot"
        )
        previous = replace(root.process, boot_id=other_boot)
        saved = recovery_registration_values(
            CustodyRecoveryRegistration(JOURNAL_ID, GENERATION, 1, previous)
        )
        async with journal_lease(root) as journal:
            await journal.run(lambda tx: tx.insert(RECOVERY, saved))
        latch = path / "recovery.lock"
        latch.write_bytes(CustodyLatch.OPEN.value)
        before = read_authority(path)

    with closing(
        CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
    ) as attached:
        with closing(CustodyLifetimeLease(attached)) as lifetime:
            with pytest.raises(CustodyUnavailable):
                await ordinary_registration(attached, lifetime)
        async with journal_lease(attached) as journal:
            retained = await journal.run(lambda tx: tx.read(RECOVERY))

    assert read_authority(path) == before
    assert retained == saved
    assert latch.read_bytes() == CustodyLatch.OPEN.value
