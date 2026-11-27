import asyncio
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import FrameType
from typing import cast

import pytest

from kora.db.connection import Database, DatabaseSettings
from kora.db.custody_journal import (
    ACTIVE_LEASES,
    CustodyJournalLease,
    CustodyJournalTransaction,
    NativeRow,
    journal_lease,
)
from kora.db.custody_models import (
    MAX_CUSTODY_COUNTER,
    MAX_CUSTODY_PARTICIPANTS,
    CustodyCommitPhase,
    CustodyFileAnchor,
    CustodyFrontier,
    CustodyIssuerKind,
    CustodyNativeOutcome,
    CustodyParticipantRegistration,
    CustodyPendingCommit,
    CustodyPostgresTransaction,
    CustodyTransactionBinding,
    CustodyUnavailable,
)
from kora.db.custody_native import (
    DESCRIPTORS,
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    CustodyRecoveryLease,
)
from kora.db.custody_recovery import CustodyRecoveryPriority
from kora.db.custody_schema import AUTHORITY, PARTICIPANT, PENDING, RECOVERY, RELATIONS
from kora.sqlite.driver import Connection as GuardedConnection
from kora.standalone.custody import NativeCustodyPlatform
from tests.api.guarded_sqlite_prebind_child import (
    held_main_descriptors,
    native_identity,
)
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    sqlite_contender,
)
from tests.db.test_custody_native import journal_path
from tests.support.databases import copy_sqlite_template, marker_only_sqlite


def participant(root: CustodyNativeDirectory, slot: int = 0) -> NativeRow:
    return {
        "slot": slot,
        "format_version": 1,
        "journal_id": b"j" * 16,
        "generation": 1,
        "issuer_sequence": slot + 1,
        **asdict(root.process),
        "gate": "open",
        "allocated_receiver": 0,
        "allocated_invocation": 0,
        "receiver_sequence": None,
        "receiver_state": None,
        "closure_revision": None,
    }


def anchor_fields(prefix: str, anchor: CustodyFileAnchor) -> NativeRow:
    return {
        f"{prefix}_device": anchor.device,
        f"{prefix}_inode": anchor.inode,
        f"{prefix}_path": anchor.canonical_path,
    }


def authority(
    root: CustodyNativeDirectory,
    allocated_commit: int,
    *,
    sqlite: CustodyFileAnchor | None = None,
    postgres: CustodyPostgresTransaction | None = None,
) -> NativeRow:
    if postgres is None:
        metadata: NativeRow = {
            "metadata_backend": "sqlite",
            **anchor_fields(
                "metadata_sqlite", sqlite or root.file_anchors["authority.sqlite3"]
            ),
            "system_identifier": None,
            "database_oid": None,
            "connection_authority_digest": None,
        }
    else:
        metadata = {
            "metadata_backend": "postgresql",
            "metadata_sqlite_device": None,
            "metadata_sqlite_inode": None,
            "metadata_sqlite_path": None,
            "system_identifier": postgres.system_identifier,
            "database_oid": postgres.database_oid,
            "connection_authority_digest": b"a" * 32,
        }
    return {
        "singleton": 1,
        "format_version": 2,
        "journal_id": b"j" * 16,
        "host_id": root.process.host_id,
        **anchor_fields("root", root.root_anchor),
        **anchor_fields("parent", root.parent_anchor),
        **anchor_fields("journal", root.file_anchors["authority.sqlite3"]),
        **anchor_fields("lifetime", root.file_anchors["lifetime.lock"]),
        **anchor_fields("commit", root.file_anchors["commit.lock"]),
        **anchor_fields("recovery", root.file_anchors["recovery.lock"]),
        **anchor_fields("journal_order", root.file_anchors["journal-order.lock"]),
        "provider_identity": b'{"store_id":"storage","version":1}',
        **anchor_fields("provider_root", root.root_anchor),
        "metadata_id": b"m" * 16,
        **metadata,
        "qualification_digest": b"q" * 32,
        "mode": "active",
        "generation": 2,
        "allocated_commit": allocated_commit,
        "allocated_issuer": 1,
        "confirmed_generation": 1,
        "confirmed_commit": 7,
        "confirmed_token": b"c" * 16,
        "recovery_priority_incarnation": None,
    }


def prepare_pending(transaction: CustodyJournalTransaction) -> CustodyPendingCommit:
    return transaction.prepare_commit(
        journal_id=b"j" * 16,
        generation=2,
        previous=CustodyFrontier(1, 7, b"c" * 16),
        issuer_kind=CustodyIssuerKind.ORDINARY,
        original=CustodyTransactionBinding(b"m" * 16, 1, 2, 3, b"s" * 32, None),
    )


@pytest.mark.asyncio
async def test_commit_preparation_persists_one_original_pending_and_allocator(
    tmp_path: Path,
) -> None:
    root = CustodyNativeDirectory(
        journal_path(tmp_path), NativeCustodyPlatform(), provision=True
    )
    row = authority(root, 12)

    try:
        async with journal_lease(root) as lease:
            await lease.initialize()
            await lease.run(lambda tx: tx.insert(AUTHORITY, row))
            pending = await lease.run(prepare_pending)
        async with journal_lease(root) as reopened:
            with pytest.raises(CustodyUnavailable):
                await reopened.run(prepare_pending)
            with pytest.raises(CustodyUnavailable):
                await reopened.run(
                    lambda tx: tx.update(PENDING, {"phase": "committed"})
                )
            saved = await reopened.run(lambda tx: tx.read(PENDING))
            allocated = await reopened.run(lambda tx: tx.read(AUTHORITY))
    finally:
        root.close()

    assert pending.phase is CustodyCommitPhase.PREPARED
    assert pending.frontier.sequence == 13
    assert pending.frontier.token != pending.previous.token
    assert saved is not None
    assert saved["token"] == pending.frontier.token
    assert saved["phase"] == "prepared"
    assert saved["previous_token"] == pending.previous.token
    assert saved["connection_sequence"] == pending.transaction.connection_sequence
    assert saved["transaction_sequence"] == pending.transaction.transaction_sequence
    assert allocated == {**row, "allocated_commit": pending.frontier.sequence}


@pytest.mark.asyncio
@pytest.mark.parametrize("allocated_commit", [12, MAX_CUSTODY_COUNTER])
async def test_commit_preparation_rolls_back_together_or_refuses_exhaustion(
    tmp_path: Path,
    allocated_commit: int,
) -> None:
    root = CustodyNativeDirectory(
        journal_path(tmp_path), NativeCustodyPlatform(), provision=True
    )
    row = authority(root, allocated_commit)

    def abort_preparation(transaction: CustodyJournalTransaction) -> None:
        prepare_pending(transaction)
        raise RuntimeError("abort before the journal commits")

    try:
        async with journal_lease(root) as lease:
            await lease.initialize()
            await lease.run(lambda tx: tx.insert(AUTHORITY, row))
            if allocated_commit == MAX_CUSTODY_COUNTER:
                with pytest.raises(CustodyUnavailable):
                    await lease.run(prepare_pending)
            else:
                with pytest.raises(
                    RuntimeError, match="abort before the journal commits"
                ):
                    await lease.run(abort_preparation)
            outcome = lease.last_outcome
            saved = await lease.run(lambda tx: tx.read(AUTHORITY))
            unprepared = await lease.run(lambda tx: tx.read(PENDING))
    finally:
        root.close()

    assert outcome is CustodyNativeOutcome.ABORTED
    assert saved == row
    assert unprepared is None


def custody_database(settings: DatabaseSettings) -> Database:
    return Database(settings, require_sqlite_full_sync=True)


@pytest.mark.asyncio
async def test_journal_enforces_native_slots_and_preserves_partial_updates(
    tmp_path: Path,
) -> None:
    baseline = set(DESCRIPTORS)
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    rows = [participant(root, slot) for slot in range(64)]

    def fill(transaction: CustodyJournalTransaction) -> None:
        for row in rows:
            transaction.insert(PARTICIPANT, row)

    try:
        async with journal_lease(root) as lease:
            await lease.initialize()
            await lease.run(fill)
            with pytest.raises(sqlite3.IntegrityError):
                await lease.run(
                    lambda tx: tx.insert(PARTICIPANT, participant(root, 64))
                )
            assert lease.last_outcome is CustodyNativeOutcome.ABORTED
            await lease.run(
                lambda tx: tx.update(PARTICIPANT, {"gate": "closing"}, key=17)
            )
            selected = await lease.run(lambda tx: tx.read(PARTICIPANT, 17))
            remaining = await lease.run(lambda tx: tx.read(PARTICIPANT, 63))
        async with journal_lease(root) as reopened:
            restored = await reopened.run(lambda tx: tx.read(PARTICIPANT, 17))
    finally:
        root.close()

    assert selected == restored == {**rows[17], "gate": "closing"}
    assert remaining == rows[63]
    assert not ACTIVE_LEASES
    assert set(DESCRIPTORS) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["participants", "issuer_sequence"])
async def test_native_registration_retains_original_slot_and_refuses_exhaustion(
    tmp_path: Path, limit: str
) -> None:
    baseline = set(DESCRIPTORS)
    root = CustodyNativeDirectory(
        journal_path(tmp_path), NativeCustodyPlatform(), provision=True
    )
    lifetime = CustodyLifetimeLease(root)
    priority = CustodyRecoveryPriority(root)
    filled = MAX_CUSTODY_PARTICIPANTS - 1 if limit == "participants" else 0
    allocated = filled if filled else MAX_CUSTODY_COUNTER - 2
    stored_authority = {**authority(root, 12), "allocated_issuer": allocated}

    def seed(transaction: CustodyJournalTransaction) -> None:
        transaction.insert(AUTHORITY, stored_authority)
        for slot in range(filled):
            transaction.insert(PARTICIPANT, participant(root, slot))

    def reserve(
        transaction: CustodyJournalTransaction,
    ) -> CustodyParticipantRegistration:
        return transaction.reserve_ordinary_participant(
            lifetime, journal_id=b"j" * 16, generation=2
        )

    try:
        async with journal_lease(root) as lease:
            await lease.initialize()
            await lease.run(seed)
        coordinator = await priority.register(journal_id=b"j" * 16, generation=2)
        async with journal_lease(root, ordinary=(lifetime, None)) as lease:
            registered = await lease.run(reserve)
            with pytest.raises(CustodyUnavailable):
                await lease.run(reserve)
            with pytest.raises(BlockingIOError), root.acquire("lifetime.lock"):
                pytest.fail(
                    "rotation entered while original lifetime ownership was live"
                )
        lifetime.close()
        async with journal_lease(root) as reopened:
            with pytest.raises(CustodyUnavailable):
                await reopened.run(reserve)
            saved = await reopened.run(lambda tx: tx.read(PARTICIPANT, filled))
            count = await reopened.run(lambda tx: tx.read(AUTHORITY))
    finally:
        lifetime.close()
        priority.close()
        root.close()

    assert registered.slot == filled
    assert coordinator.incarnation == allocated + 1
    assert registered.issuer_sequence == allocated + 2
    assert registered.process == root.process
    assert saved is not None
    assert saved["issuer_sequence"] == registered.issuer_sequence
    assert saved["gate"] == "open"
    assert saved["allocated_receiver"] == saved["allocated_invocation"] == 0
    assert count == {**stored_authority, "allocated_issuer": registered.issuer_sequence}
    assert set(DESCRIPTORS) == baseline


@pytest.mark.asyncio
async def test_reserved_coordinator_can_register_at_full_ordinary_capacity(
    tmp_path: Path,
) -> None:
    baseline = set(DESCRIPTORS)
    root = CustodyNativeDirectory(
        journal_path(tmp_path), NativeCustodyPlatform(), provision=True
    )
    coordinator = CustodyRecoveryLease(root)
    lifetime = CustodyLifetimeLease(root)
    reserved = MAX_CUSTODY_PARTICIPANTS
    rows = tuple(participant(root, slot) for slot in range(reserved))
    stored_authority = {**authority(root, 12), "allocated_issuer": reserved}

    def seed(transaction: CustodyJournalTransaction) -> None:
        transaction.insert(AUTHORITY, stored_authority)
        for row in rows:
            transaction.insert(PARTICIPANT, row)

    try:
        async with journal_lease(root) as lease:
            await lease.initialize()
            await lease.run(seed)
            pending = await lease.run(prepare_pending)
            old_pending = await lease.run(lambda tx: tx.read(PENDING))
            old_frontier = await lease.run(lambda tx: tx.read(AUTHORITY))

            selected = await lease.run(
                lambda tx: tx.reserve_recovery_coordinator(
                    coordinator, lifetime, journal_id=b"j" * 16, generation=2
                )
            )
            with pytest.raises(CustodyUnavailable):
                await lease.run(
                    lambda tx: tx.reserve_recovery_coordinator(
                        coordinator, lifetime, journal_id=b"j" * 16, generation=2
                    )
                )
            with pytest.raises(BlockingIOError), root.acquire("recovery.lock"):
                pytest.fail("a competing recovery coordinator acquired the live lease")
            after = await lease.run(lambda tx: tx.read(AUTHORITY))
            held_pending = await lease.run(lambda tx: tx.read(PENDING))
            saved = await lease.run(lambda tx: tx.read(RECOVERY))
            ordinary = await lease.run(
                lambda tx: tuple(tx.read(PARTICIPANT, slot) for slot in range(reserved))
            )
    finally:
        lifetime.close()
        coordinator.close()
        root.close()

    assert selected.incarnation == reserved + 1
    assert selected.process == root.process
    assert old_pending == held_pending
    assert held_pending is not None
    assert held_pending["token"] == pending.frontier.token
    assert old_frontier is not None
    assert after == {**old_frontier, "allocated_issuer": selected.incarnation}
    assert saved is not None
    assert saved["incarnation"] == selected.incarnation
    assert saved["invocation_sequence"] is None
    assert ordinary == rows
    assert set(DESCRIPTORS) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["oversized", "version", "closed_without_evidence"]
)
async def test_journal_bad_record_never_appears_absent(
    tmp_path: Path, corruption: str
) -> None:
    baseline = set(DESCRIPTORS)
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    try:
        async with journal_lease(root) as lease:
            await lease.initialize()
            await lease.run(lambda tx: tx.insert(PARTICIPANT, participant(root)))
        with sqlite_contender(path / "authority.sqlite3") as raw:
            with closing(raw.execute("PRAGMA ignore_check_constraints=ON")):
                pass
            if corruption == "oversized":
                statement = "UPDATE participant SET host_id = ? WHERE slot = 0"
                values: tuple[str | int, ...] = ("x" * 65536,)
            elif corruption == "version":
                statement, values = "UPDATE participant SET format_version = ?", (2,)
            else:
                statement, values = "UPDATE participant SET gate = ?", ("closed",)
            with closing(raw.execute(statement, values)):
                pass
            raw.commit()

        async with journal_lease(root) as reopened:
            with pytest.raises(CustodyUnavailable):
                await reopened.run(lambda tx: tx.read(PARTICIPANT, 0))
            assert reopened.last_outcome is CustodyNativeOutcome.ABORTED
    finally:
        root.close()

    assert not ACTIVE_LEASES
    assert set(DESCRIPTORS) == baseline


@pytest.mark.asyncio
async def test_journal_cancel_keeps_original_native_job_and_commit_lock(
    tmp_path: Path,
) -> None:
    baseline = set(DESCRIPTORS)
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    parked = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    row = participant(root)

    def store(transaction: CustodyJournalTransaction) -> None:
        transaction.insert(PARTICIPANT, row)
        loop.call_soon_threadsafe(parked.set)
        if not release.wait(10):
            raise TimeoutError("native journal test barrier did not close")

    try:
        async with journal_lease(root) as lease:
            await lease.initialize()
            task = asyncio.create_task(lease.run(store))
            try:
                await asyncio.wait_for(parked.wait(), 10)
                task.cancel()
                turn = asyncio.Event()
                loop.call_soon(turn.set)
                await asyncio.wait_for(turn.wait(), 10)
                with pytest.raises(BlockingIOError):
                    root.lock("commit.lock")
                with pytest.raises(CustodyUnavailable):
                    await lease.run(lambda tx: tx.read(PARTICIPANT, 0))
                assert not task.done()
                assert len(ACTIVE_LEASES) == 1
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            outcome = lease.last_outcome
            restored = await lease.run(lambda tx: tx.read(PARTICIPANT, 0))
    finally:
        root.close()

    assert outcome is CustodyNativeOutcome.COMMITTED
    assert restored == row
    assert not ACTIVE_LEASES
    assert set(DESCRIPTORS) == baseline


@pytest.mark.asyncio
async def test_journal_schema_and_physical_profile_survive_reopen(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    try:
        with closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
        ) as existing:
            async with journal_lease(existing) as lease:
                with pytest.raises(CustodyUnavailable):
                    await lease.initialize()
        assert (path / "authority.sqlite3").stat().st_size == 0
        async with journal_lease(root) as lease:
            await lease.initialize()
        with sqlite_contender(path / "authority.sqlite3") as raw:
            with closing(
                raw.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            ) as cursor:
                names = {
                    str(cast("tuple[object]", row)[0]) for row in cursor.fetchall()
                }
            with closing(raw.execute("PRAGMA journal_mode")) as cursor:
                mode = cursor.fetchone()
            with closing(raw.execute("PRAGMA page_size")) as cursor:
                page_size = cursor.fetchone()
        async with journal_lease(root) as lease:
            absent = await lease.run(lambda tx: tx.read(PARTICIPANT, 0))
    finally:
        root.close()

    assert names == {relation.name for relation in RELATIONS}
    assert mode == ("delete",)
    assert page_size == (4096,)
    assert absent is None
    assert not ACTIVE_LEASES


@pytest.mark.parametrize("case", ["KMT1", "KJT1", "main", "sidecar"])
def test_guarded_authority_journal_refusal_retains_original_j_until_native_retirement(
    tmp_path: Path, sqlite_template: Path, case: str
) -> None:
    asyncio.run(exercise_authority_journal_refusal(tmp_path, sqlite_template, case))


async def exercise_authority_journal_refusal(
    tmp_path: Path, sqlite_template: Path, case: str
) -> None:
    path = journal_path(tmp_path)
    with closing(
        CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    ) as root:
        main = path / "authority.sqlite3"
        if case in {"KMT1", "KJT1"}:
            await assert_authority_marker_refused(root, tmp_path, case.encode())
            return

        async with journal_lease(root) as lease:
            await lease.initialize()
        incumbent_path = main if case == "main" else tmp_path / "incumbent.sqlite"
        if case == "sidecar":
            copy_sqlite_template(sqlite_template, incumbent_path)
        await assert_authority_refusal_retains(root, incumbent_path, case == "sidecar")


async def assert_authority_marker_refused(
    root: CustodyNativeDirectory, scratch: Path, marker: bytes
) -> None:
    main = root.path / "authority.sqlite3"
    original = marker_only_sqlite(scratch / "isolated.sqlite", marker)
    main.write_bytes(original)
    sidecars = [Path(f"{main}{ending}") for ending in ("-wal", "-shm", "-journal")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"owned sentinel")
    calls: list[str] = []
    watched = {
        CustodyNativeDirectory.admit_journal_main.__code__,
        CustodyNativeDirectory.admit_rollback_journal.__code__,
        GuardedConnection.execute.__code__,
    }

    def observe(frame: FrameType, event: str, result: object) -> None:
        if event == "call" and frame.f_code in watched:
            calls.append(frame.f_code.co_name)

    previous = threading.getprofile()
    threading.setprofile(observe)
    try:
        with pytest.raises(sqlite3.DatabaseError) as failure:
            async with journal_lease(root) as lease:
                await lease.initialize()
    finally:
        threading.setprofile(previous)
    assert failure.value.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert not calls
    assert main.read_bytes() == original
    assert [path.read_bytes() for path in sidecars] == [b"owned sentinel"] * 3
    assert not held_main_descriptors(native_identity(main))
    assert not ACTIVE_LEASES
    with root.acquire("commit.lock"), root.acquire("journal-order.lock"):
        pass


async def assert_authority_refusal_retains(
    root: CustodyNativeDirectory, incumbent_path: Path, sidecar: bool
) -> None:
    identity = native_identity(incumbent_path)
    journal = root.path / "authority.sqlite3-journal"
    charged: list[str] = []
    released = asyncio.Event()
    admitted: list[CustodyJournalLease] = []

    def enter() -> None:
        charged.append("entered")

    async def leave() -> None:
        with root.acquire("journal-order.lock"), root.acquire("commit.lock"):
            assert not held_main_descriptors(identity)
        charged.append("released")
        released.set()

    async def refuse_original() -> None:
        async with journal_lease(root, sqlite_accounting=(enter, leave)) as exact:
            admitted.append(exact)
            if sidecar:
                await exact.run(lambda tx: tx.read(AUTHORITY))
            else:
                await exact.initialize()

    with sqlite_contender(incumbent_path) as incumbent:
        incumbent.execute("BEGIN IMMEDIATE").close()
        original_fds = held_main_descriptors(identity)
        assert len(original_fds) == 1
        if sidecar:
            journal.hardlink_to(incumbent_path)
        with pytest.raises((CustodyUnavailable, sqlite3.Error)):
            await refuse_original()
        retained = held_main_descriptors(identity)
        assert len(retained) == 2
        assert set(original_fds) < set(retained)
        (exact,) = admitted
        assert exact in ACTIVE_LEASES
        assert charged == ["entered"]
        for name in ("journal-order.lock", "commit.lock"):
            with pytest.raises(BlockingIOError), root.acquire(name):
                pytest.fail("the admitted original lock was released early")

        def close_incumbent() -> None:
            incumbent.commit()
            incumbent.close()

        await asyncio.to_thread(close_incumbent)
        await asyncio.wait_for(released.wait(), CONDITION_DEADLINE_SECONDS)
        assert exact not in ACTIVE_LEASES
        assert charged == ["entered", "released"]
        if sidecar:
            journal.unlink()

    async with journal_lease(root) as healthy:
        assert await healthy.run(lambda tx: tx.read(AUTHORITY)) is None
