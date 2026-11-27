import asyncio
import errno
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from contextlib import AsyncExitStack, asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from pathlib import Path
from types import FrameType
from typing import cast

import aiosqlite.core
import pytest
import pytest_asyncio
from sqlalchemy import event, insert
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import Pool, PoolResetState
from sqlalchemy.pool.base import ConnectionPoolEntry, PoolProxiedConnection
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db import CLOSE_TIMED_OUT, DB_COMPONENT
from kora.db.connection import (
    Database,
    DatabaseSettings,
    DatabaseWriterFailure,
    DatabaseWriterRejected,
    Lifecycle,
    LocalSQLiteWriterUnit,
    SqliteDriverConnection,
    open_sqlite_file,
)
from kora.db.custody_frontier_v1 import custody_frontier
from kora.db.custody_journal import (
    CustodyJournalTransaction,
    NativeRow,
    journal_lease,
)
from kora.db.custody_models import (
    ZERO_CUSTODY_ID,
    CustodyFrontier,
    CustodyParticipantRegistration,
    CustodyPendingCommit,
    CustodyUnavailable,
    custody_counter,
    custody_identity,
)
from kora.db.custody_native import (
    CustodyLatch,
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    file_anchor,
)
from kora.db.custody_recovery import CustodyRecoveryPriority
from kora.db.custody_schema import AUTHORITY
from kora.db.custody_sqlite import CustodySQLiteConnection, CustodySQLiteRoot
from kora.db.custody_writer import CustodyDriverConnection, LocalSQLiteWriterLoan
from kora.db.token_statements import REVOKED
from kora.observability import LoggingSettings
from kora.sqlite.driver import Cursor
from kora.standalone.custody import NativeCustodyPlatform, local_sqlite_writer
from tests.api.guarded_sqlite_prebind_child import (
    held_main_descriptors,
    native_identity,
)
from tests.db.closing import CONDITION_DEADLINE_SECONDS, sqlite_contender, until
from tests.db.seed import FakeClock, Seeder
from tests.db.test_custody_journal import authority
from tests.db.test_custody_native import journal_path
from tests.db.test_custody_recovery_priority import (
    GENERATION,
    JOURNAL_ID,
    initialize,
    ordinary_registration,
    read_authority,
)
from tests.db.test_read_snapshot import assert_native_closed
from tests.support.databases import copy_sqlite_template, sqlite_settings

METADATA_ID = b"m" * 16
FIRST_ALLOCATION = 13
CONFIRMED = CustodyFrontier(1, 7, b"c" * 16)
CONFLICT_DIGEST = "0" * 64


@dataclass
class PoolObservations:
    opened: list[object] = field(default_factory=list[object])
    checked_out: list[object] = field(default_factory=list[object])
    checked_in: list[object] = field(default_factory=list[object])
    closed: list[object] = field(default_factory=list[object])

    def connect(self, connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        self.opened.append(record.driver_connection)

    def checkout(
        self,
        connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        proxy: PoolProxiedConnection,
    ) -> None:
        self.checked_out.append(record.driver_connection)

    def checkin(
        self, connection: DBAPIConnection | None, record: ConnectionPoolEntry
    ) -> None:
        self.checked_in.append(record.driver_connection)

    def close(self, connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        self.closed.append(record.driver_connection)


@contextmanager
def observe_pool() -> Generator[PoolObservations]:
    observations = PoolObservations()
    callbacks = (
        ("connect", observations.connect),
        ("checkout", observations.checkout),
        ("checkin", observations.checkin),
        ("close", observations.close),
    )
    for name, callback in callbacks:
        event.listen(Pool, name, callback)
    try:
        yield observations
    finally:
        for name, callback in callbacks:
            event.remove(Pool, name, callback)


@dataclass(frozen=True)
class MetadataState:
    frontier: CustodyFrontier
    previous: CustodyFrontier
    revoked: tuple[bool, ...]


async def seed_metadata(database: Database) -> tuple[str, str, str]:
    try:
        async with database.transaction() as writer:
            await writer.run_sync(
                lambda connection: custody_frontier.create(connection)
            )
            await writer.execute(
                insert(custody_frontier).values(
                    singleton=1,
                    format_version=1,
                    metadata_id=METADATA_ID,
                    journal_id=JOURNAL_ID,
                    generation=CONFIRMED.generation,
                    commit_sequence=CONFIRMED.sequence,
                    token=CONFIRMED.token,
                    previous_generation=0,
                    previous_commit=0,
                    previous_token=ZERO_CUSTODY_ID,
                )
            )
            seeder = Seeder(writer)
            return (
                await seeder.token(CONFLICT_DIGEST),
                await seeder.token("1" * 64),
                await seeder.token("2" * 64),
            )
    finally:
        await database.close()


def read_metadata(path: Path) -> MetadataState:
    with sqlite_contender(path, readonly=True) as reader:
        with closing(
            reader.execute(
                "SELECT generation, commit_sequence, token, "
                "previous_generation, previous_commit, previous_token "
                "FROM custody_frontier WHERE singleton=1"
            )
        ) as cursor:
            row = cast("tuple[object, ...] | None", cursor.fetchone())
        expiry_query = "SELECT expires_at FROM api_token ORDER BY digest"
        with closing(reader.execute(expiry_query)) as cursor:
            expiry = tuple(cast("tuple[object]", row)[0] for row in cursor)
        expected = REVOKED.isoformat(timespec="microseconds")
        assert set(expiry) <= {None, expected}
        revoked = tuple(value == expected for value in expiry)
    assert row is not None
    return MetadataState(
        CustodyFrontier(
            custody_counter(row[0]), custody_counter(row[1]), custody_identity(row[2])
        ),
        CustodyFrontier(
            custody_counter(row[3], allow_zero=True),
            custody_counter(row[4], allow_zero=True),
            custody_identity(row[5], allow_zero=True),
        ),
        revoked,
    )


def read_pending(root: CustodyNativeDirectory) -> tuple[object, ...] | None:
    query = "SELECT phase, commit_sequence, token FROM pending_commit"
    with (
        sqlite_contender(root.path / "authority.sqlite3", readonly=True) as observer,
        closing(observer.execute(query)) as cursor,
    ):
        row = cast("tuple[object, ...] | None", cursor.fetchone())
        return row


def try_metadata_writer(path: Path) -> None:
    with sqlite_contender(path) as database:
        with closing(database.execute("BEGIN IMMEDIATE")):
            pass
        database.rollback()


def assert_open_ownership(root: CustodyNativeDirectory, metadata: Path) -> None:
    assert_commit_lock_owned(root)
    with pytest.raises(sqlite3.OperationalError) as blocked:
        try_metadata_writer(metadata)
    assert blocked.value.sqlite_errorcode == sqlite3.SQLITE_BUSY


def assert_commit_lock_owned(root: CustodyNativeDirectory) -> None:
    with pytest.raises(BlockingIOError), root.acquire("commit.lock"):
        pytest.fail("the original operation released C")


def assert_released(root: CustodyNativeDirectory, metadata: Path) -> None:
    with root.acquire("commit.lock"), root.acquire("journal-order.lock"):
        try_metadata_writer(metadata)


def install_conflicting_token_trigger(path: Path) -> None:
    with (
        sqlite_contender(path) as fixture,
        closing(
            fixture.execute(
                "CREATE TRIGGER native_token_rollback BEFORE UPDATE OF expires_at "
                "ON api_token WHEN OLD.digest = '"
                + CONFLICT_DIGEST
                + "' BEGIN SELECT RAISE(ROLLBACK, 'original writer rolled back'); END"
            )
        ),
    ):
        pass


def install_frontier_mismatch_trigger(path: Path) -> None:
    with (
        sqlite_contender(path) as fixture,
        closing(
            fixture.execute(
                "CREATE TRIGGER native_frontier_mismatch BEFORE UPDATE "
                "ON custody_frontier BEGIN SELECT RAISE(IGNORE); END"
            )
        ),
    ):
        pass


def assert_confirmed(authority_row: NativeRow, frontier: CustodyFrontier) -> None:
    assert authority_row["confirmed_generation"] == frontier.generation
    assert authority_row["confirmed_commit"] == frontier.sequence
    assert authority_row["confirmed_token"] == frontier.token


class WriterCut(Enum):
    ADMISSION = auto()
    NATIVE_CLOSE = auto()
    COLD_MAIN_ADMISSION = auto()
    DISPATCHING = auto()
    MUTATION = auto()
    PREPARING = auto()
    PREPARED = auto()
    FRONTIER_ADVANCED = auto()
    RECORDING = auto()
    COMPACTING = auto()


class WriterFault(Enum):
    INNER_WITNESS = auto()
    OUTER_WITNESS = auto()
    FINAL_SYNCHRONIZATION = auto()
    FRONTIER_CAS = auto()


@dataclass
class WriterProfile:
    cut: WriterCut | None = None
    fault: WriterFault | None = None
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    statements: list[str] = field(default_factory=list[str])
    mutation_ids: list[str] = field(default_factory=list[str])
    mutations_completed: list[bool] = field(default_factory=list[bool])
    mutation_failures: list[bool] = field(default_factory=list[bool])
    compacted: list[CustodyPendingCommit] = field(
        default_factory=list[CustodyPendingCommit]
    )
    closed_connections: list[CustodySQLiteConnection] = field(
        default_factory=list[CustodySQLiteConnection]
    )
    fixture_native_changes: list[str] = field(default_factory=list[str])
    _prepared_thread: threading.Thread | None = field(default=None, init=False)

    def observe(self, frame: FrameType, event: str, operation: object) -> None:
        code = frame.f_code
        cut = self.cut
        if code is CustodySQLiteConnection.close.__code__:
            self._observe_retirement(frame, event)
        if event == "call" and (
            (
                code is CustodySQLiteConnection.admit_main.__code__
                and cut is WriterCut.COLD_MAIN_ADMISSION
            )
            or (
                code is CustodyJournalTransaction.permit_original_dispatch.__code__
                and cut is WriterCut.DISPATCHING
            )
        ):
            self._pause()
        if code.co_qualname == "CustodySQLiteRoot._execute":
            self._observe_statement(frame, operation, event)
        if (
            event == "call"
            and code is CustodyJournalTransaction.require_ordinary_checkout.__code__
            and cut is WriterCut.ADMISSION
        ):
            self._pause()
        elif (
            event == "call"
            and code is CustodyJournalTransaction.compact_original_commit.__code__
        ):
            compacted: object = frame.f_locals.get("pending")
            assert isinstance(compacted, CustodyPendingCommit)
            self.compacted.append(compacted)
            if cut is WriterCut.COMPACTING:
                self._pause()
        elif (
            code is CustodyJournalTransaction.prepare_commit.__code__
            and event == "call"
        ):
            if cut is WriterCut.PREPARING:
                self._pause()
            if cut is WriterCut.PREPARED:
                self._prepared_thread = threading.current_thread()
        elif (
            cut is WriterCut.PREPARED
            and code.co_qualname == "CustodyJournalLease._perform"
            and event == "return"
            and self._prepared_thread is threading.current_thread()
        ):
            self._pause()
        elif (
            event == "call"
            and cut is WriterCut.RECORDING
            and code is CustodyJournalTransaction.record_original_outcome.__code__
        ):
            self._pause()

    def _observe_retirement(self, frame: FrameType, event: str) -> None:
        native: object = frame.f_locals.get("self")
        if not isinstance(native, CustodySQLiteConnection):
            return
        if event == "call" and self.cut is WriterCut.NATIVE_CLOSE:
            self._pause()
        if event == "return" and native.settled():
            self.closed_connections.append(native)

    def _observe_statement(
        self, frame: FrameType, operation: object, event: str
    ) -> None:
        root: object = frame.f_locals.get("self")
        connection: object = getattr(root, "_connection", None)
        assert isinstance(connection, CustodySQLiteConnection)
        statement: object = frame.f_locals.get("statement")
        assert isinstance(statement, str)
        mutation = statement.startswith("UPDATE api_token ")
        if event == "return":
            if mutation:
                outcomes = (
                    self.mutations_completed
                    if isinstance(operation, Cursor)
                    else self.mutation_failures
                )
                outcomes.append(connection.in_transaction)
            if self.cut is WriterCut.FRONTIER_ADVANCED and statement.startswith(
                "UPDATE main.custody_frontier "
            ):
                self._pause()
            return
        if event != "call":
            return
        self._apply_native_fault(frame, connection, statement)
        self.statements.append(statement)
        if mutation:
            parameters: object = frame.f_locals.get("parameters")
            assert isinstance(parameters, dict)
            token_id = cast("dict[str, object]", parameters)["token_id"]
            assert isinstance(token_id, str)
            self.mutation_ids.append(token_id)
            if self.cut is WriterCut.MUTATION:
                self._pause()

    def _apply_native_fault(
        self, frame: FrameType, connection: CustodySQLiteConnection, statement: str
    ) -> None:
        fault = self.fault
        if (
            fault is WriterFault.INNER_WITNESS
            and statement.startswith("RELEASE custody_inner_")
        ) or (
            fault is WriterFault.OUTER_WITNESS
            and statement.startswith("RELEASE custody_outer_")
        ):
            change = statement
        elif (
            fault is WriterFault.FINAL_SYNCHRONIZATION
            and statement == "PRAGMA fullfsync"
            and inside_sqlite_commit(frame)
        ):
            with closing(connection.execute(statement)) as cursor:
                old: object = cursor.fetchone()
            assert old in ((0,), (1,))
            change = "PRAGMA fullfsync=ON" if old == (0,) else "PRAGMA fullfsync=OFF"
        else:
            return
        assert connection.in_transaction
        with closing(connection.execute(change)):
            pass
        assert connection.in_transaction
        self.fixture_native_changes.append(change)
        self.fault = None

    def _pause(self) -> None:
        self.cut = None
        self.entered.set()
        if not self.release.wait(CONDITION_DEADLINE_SECONDS):
            raise TimeoutError("the original writer fixture was not released")


@contextmanager
def observe_writer(
    cut: WriterCut | None = None, fault: WriterFault | None = None
) -> Generator[WriterProfile]:
    profile = WriterProfile(cut, fault)
    previous = threading.getprofile()
    threading.setprofile(profile.observe)
    try:
        yield profile
    finally:
        profile.release.set()
        threading.setprofile(previous)


def inside_sqlite_commit(frame: FrameType) -> bool:
    ancestor: FrameType | None = frame
    while ancestor is not None:
        if ancestor.f_code is CustodySQLiteRoot.commit.__code__:
            return True
        ancestor = ancestor.f_back
    return False


async def deliver_cancellation(task: asyncio.Task[None] | asyncio.Task[int]) -> None:
    turn = asyncio.Event()
    assert task.cancel()
    asyncio.get_running_loop().call_soon(turn.set)
    await asyncio.wait_for(turn.wait(), CONDITION_DEADLINE_SECONDS)


def assert_native_commands(profile: WriterProfile, expected: tuple[str, ...]) -> None:
    commands: list[str] = []
    for sql in profile.statements:
        for prefix in (
            "SAVEPOINT custody_outer",
            "SAVEPOINT custody_inner",
            "UPDATE api_token",
            "RELEASE custody_inner",
            "UPDATE main.custody_frontier",
            "RELEASE custody_outer",
            "COMMIT",
            "ROLLBACK",
        ):
            if sql.startswith(prefix):
                commands.append(prefix)
                break
    assert tuple(commands) == expected


ORIGINAL_COMMIT = (
    "SAVEPOINT custody_outer",
    "SAVEPOINT custody_inner",
    "UPDATE api_token",
    "RELEASE custody_inner",
    "UPDATE main.custody_frontier",
    "RELEASE custody_outer",
    "COMMIT",
)


@dataclass(frozen=True)
class WriterOwners:
    settings: DatabaseSettings
    root: CustodyNativeDirectory
    priority: CustodyRecoveryPriority
    lifetime: CustodyLifetimeLease
    participant: CustodyParticipantRegistration
    tokens: tuple[str, str, str]


@asynccontextmanager
async def open_writer_owners(
    tmp_path: Path, sqlite_template: Path
) -> AsyncGenerator[WriterOwners]:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.db")
    metadata = settings.sqlite_path.resolve(strict=True)
    metadata_anchor = file_anchor(metadata.stat(), metadata)
    path = journal_path(tmp_path)
    tokens = await seed_metadata(Database(settings))
    with (
        closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
        ) as root,
        closing(CustodyRecoveryPriority(root)) as coordinator,
        closing(CustodyLifetimeLease(root)) as lifetime,
    ):
        async with journal_lease(root) as journal:
            await journal.initialize()
            await journal.run(
                lambda tx: tx.insert(
                    AUTHORITY,
                    authority(root, FIRST_ALLOCATION - 1, sqlite=metadata_anchor),
                )
            )
        reserved = await coordinator.register(
            journal_id=JOURNAL_ID, generation=GENERATION
        )
        assert reserved.generation == GENERATION
        assert root.read_latch() is CustodyLatch.OPEN
        participant = await ordinary_registration(root, lifetime)
        yield WriterOwners(settings, root, coordinator, lifetime, participant, tokens)


@pytest_asyncio.fixture
async def writer_owners(
    tmp_path: Path, sqlite_template: Path
) -> AsyncIterator[WriterOwners]:
    async with open_writer_owners(tmp_path, sqlite_template) as owners:
        yield owners


@pytest.mark.asyncio
async def test_custody_sqlite_witnesses_guard_the_original_root(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    settings, root, lifetime, participant = (
        owners.settings,
        owners.root,
        owners.lifetime,
        owners.participant,
    )
    first, second, third = owners.tokens
    database, entry = local_sqlite_writer(settings, root, lifetime, participant)
    metadata = settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)

    with observe_writer() as native, observe_pool() as pool:
        try:
            async with database.transaction(_local_sqlite_writer=entry) as first_loan:
                first_count = await first_loan.revoke_token(first)
                assert not any(
                    hasattr(first_loan, capability)
                    for capability in (
                        "execute",
                        "engine",
                        "connection",
                        "cursor",
                        "commit",
                        "rollback",
                        "begin",
                        "begin_nested",
                    )
                )
                assert_open_ownership(root, metadata)
                assert read_metadata(metadata) == original
                assert read_pending(root) is None
            after_first = read_metadata(metadata)
            first_journal = read_authority(root.path)
            assert_released(root, metadata)

            deadline = asyncio.get_running_loop().time() + 10.0
            async with database.transaction(
                admission_deadline=deadline, _local_sqlite_writer=entry
            ) as timed_loan:
                second_count = await timed_loan.revoke_token(second)
            after_second = read_metadata(metadata)
            assert_released(root, metadata)

            third_count = await database.run(LocalSQLiteWriterUnit(entry, third))
            after_third = read_metadata(metadata)
            final_journal = read_authority(root.path)
        finally:
            await database.close()

    assert (first_count, second_count, third_count) == (1, 1, 1)
    assert after_first.previous == original.frontier == CONFIRMED
    assert after_first.revoked == (True, False, False)
    assert after_second.previous == after_first.frontier
    assert after_second.revoked == (True, True, False)
    assert after_third.previous == after_second.frontier
    assert after_third.revoked == (True, True, True)
    assert tuple(
        (state.frontier.generation, state.frontier.sequence)
        for state in (after_first, after_second, after_third)
    ) == ((GENERATION, 13), (GENERATION, 14), (GENERATION, 15))
    assert (
        len(
            {state.frontier.token for state in (after_first, after_second, after_third)}
        )
        == 3
    )
    assert_confirmed(first_journal, after_first.frontier)
    assert_confirmed(final_journal, after_third.frontier)
    assert final_journal["allocated_commit"] == 15
    assert read_pending(root) is None
    assert pool.checked_out == pool.checked_in == pool.opened * 3
    assert pool.opened == pool.closed
    assert len(pool.opened) == 1
    assert first_loan is not timed_loan
    assert_native_commands(native, ORIGINAL_COMMIT * 3)
    assert native.mutation_ids == [first, second, third]
    for witness in ("outer", "inner"):
        created = [
            sql.removeprefix("SAVEPOINT ")
            for sql in native.statements
            if sql.startswith(f"SAVEPOINT custody_{witness}_")
        ]
        released = [
            sql.removeprefix("RELEASE ")
            for sql in native.statements
            if sql.startswith(f"RELEASE custody_{witness}_")
        ]
        assert released == created
        assert len(set(created)) == 3
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert_released(root, metadata)


@pytest.mark.asyncio
async def test_journal_settles_only_after_exact_writer_release_and_durable_compaction(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    root = owners.root
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    database, entry = local_sqlite_writer(
        owners.settings, root, owners.lifetime, owners.participant
    )
    at_reset, release = asyncio.Event(), asyncio.Event()

    def before_checkin(
        connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        state: PoolResetState,
    ) -> None:
        if isinstance(record.driver_connection, CustodyDriverConnection):
            at_reset.set()
            await_(release.wait())

    with observe_pool() as pool, observe_writer() as native:
        event.listen(Pool, "reset", before_checkin)
        task = asyncio.create_task(
            database.run(LocalSQLiteWriterUnit(entry, owners.tokens[0]))
        )
        try:
            await asyncio.wait_for(at_reset.wait(), CONDITION_DEADLINE_SECONDS)
            assert pool.checked_out == pool.opened
            assert len(pool.checked_out) == 1
            assert not pool.checked_in
            assert not task.done()
            assert database.units_in_flight() == 1
            assert not database.writer_is_free()
            assert_commit_lock_owned(root)
            assert_native_commands(native, ORIGINAL_COMMIT)
            committed = read_metadata(metadata)
            assert committed.revoked == (True, False, False)
            assert read_pending(root) == (
                "dispatching",
                committed.frontier.sequence,
                committed.frontier.token,
            )
            assert_confirmed(read_authority(root.path), CONFIRMED)
            release.set()
            assert (
                await asyncio.wait_for(asyncio.shield(task), CONDITION_DEADLINE_SECONDS)
                == 1
            )
            assert pool.checked_in == pool.checked_out
            assert read_pending(root) is None
            assert_confirmed(read_authority(root.path), committed.frontier)
            assert database.units_in_flight() == 0
            assert database.writer_is_free()
            assert_released(root, metadata)
        finally:
            release.set()
            try:
                await task
            finally:
                event.remove(Pool, "reset", before_checkin)
                await database.close()
        assert pool.closed == pool.checked_out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "commands"),
    [
        (WriterFault.INNER_WITNESS, (*ORIGINAL_COMMIT[:4], "ROLLBACK")),
        (WriterFault.OUTER_WITNESS, (*ORIGINAL_COMMIT[:6], "ROLLBACK")),
        (WriterFault.FINAL_SYNCHRONIZATION, (*ORIGINAL_COMMIT[:5], "ROLLBACK")),
        (WriterFault.FRONTIER_CAS, (*ORIGINAL_COMMIT[:5], "ROLLBACK")),
    ],
)
async def test_custody_sqlite_refuses_incomplete_native_commit(
    writer_owners: WriterOwners, fault: WriterFault, commands: tuple[str, ...]
) -> None:
    owners = writer_owners
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    if fault is WriterFault.FRONTIER_CAS:
        install_frontier_mismatch_trigger(metadata)
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    original = read_metadata(metadata)
    first = owners.tokens[0]

    async def write() -> None:
        async with database.transaction(_local_sqlite_writer=entry) as loan:
            assert await loan.revoke_token(first) == 1

    with observe_writer(fault=fault) as native, observe_pool() as pool:
        try:
            with pytest.raises(CustodyUnavailable):
                await write()
            after = read_metadata(metadata)
            saved = read_authority(owners.root.path)
        finally:
            await database.close()

    assert_native_commands(native, commands)
    assert native.mutation_ids == [first]
    assert len(native.fixture_native_changes) == int(
        fault is not WriterFault.FRONTIER_CAS
    )
    assert after == original
    assert_confirmed(saved, original.frontier)
    assert saved["allocated_commit"] == FIRST_ALLOCATION
    assert read_pending(owners.root) is None
    assert len(pool.checked_out) == len(pool.checked_in) == 1
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert_released(owners.root, metadata)


@pytest.mark.asyncio
async def test_custody_sqlite_refuses_mutation_after_native_root_loss(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    settings, root, lifetime, participant = (
        owners.settings,
        owners.root,
        owners.lifetime,
        owners.participant,
    )
    conflicting, next_token, later_token = owners.tokens
    metadata = settings.sqlite_path.resolve(strict=True)
    install_conflicting_token_trigger(metadata)
    database, entry = local_sqlite_writer(settings, root, lifetime, participant)
    original = read_metadata(metadata)

    with observe_writer() as native, observe_pool() as pool:
        try:
            async with AsyncExitStack() as scope:
                loan = await scope.enter_async_context(
                    database.transaction(_local_sqlite_writer=entry)
                )
                with pytest.raises(CustodyUnavailable):
                    await loan.revoke_token(conflicting, next_token)
                with pytest.raises(CustodyUnavailable):
                    await loan.revoke_token(later_token)
                with pytest.raises(BlockingIOError), root.acquire("commit.lock"):
                    pytest.fail("native rollback released C before finalization")
                assert len(pool.checked_out) == 1
                assert pool.checked_in == []
                with pytest.raises(CustodyUnavailable):
                    await scope.aclose()
            with pytest.raises(CustodyUnavailable):
                await loan.revoke_token(later_token)
            after = read_metadata(metadata)
            saved = read_authority(root.path)
        finally:
            await database.close()

    assert native.mutation_ids == [conflicting]
    assert native.mutation_failures == [False]
    assert_native_commands(native, ORIGINAL_COMMIT[:3])
    assert after == original
    assert_confirmed(saved, CONFIRMED)
    assert saved["allocated_commit"] == FIRST_ALLOCATION - 1
    assert read_pending(root) is None
    assert len(pool.checked_out) == len(pool.checked_in) == 1
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert_released(root, metadata)


@pytest.mark.asyncio
async def test_custody_writer_refuses_unqualified_native_entry(
    tmp_path: Path,
    sqlite_template: Path,
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    root, priority, lifetime = owners.root, owners.priority, owners.lifetime
    participant, settings = owners.participant, owners.settings
    token_id = owners.tokens[0]
    metadata = settings.sqlite_path.resolve(strict=True)
    ordinary = Database(settings)
    database, entry = local_sqlite_writer(settings, root, lifetime, participant)
    other_database, other_entry = local_sqlite_writer(
        settings, root, lifetime, participant
    )
    other_settings = copy_sqlite_template(sqlite_template, tmp_path / "different.db")
    original = read_metadata(metadata)
    missing = replace(participant, issuer_sequence=participant.issuer_sequence + 1)

    async def attempt_unqualified(
        selected: DatabaseSettings,
        registered: CustodyParticipantRegistration,
    ) -> None:
        candidate, candidate_entry = local_sqlite_writer(
            selected, root, lifetime, registered
        )
        try:
            async with candidate.transaction(_local_sqlite_writer=candidate_entry):
                pytest.fail("an invalid configuration borrowed the scoped writer")
        finally:
            await candidate.close()

    with observe_pool() as pool:
        try:
            with pytest.raises(CustodyUnavailable):
                async with database.transaction():
                    pytest.fail("a missing entry borrowed the scoped writer")
            with pytest.raises(CustodyUnavailable):
                async with database.transaction(_local_sqlite_writer=other_entry):
                    pytest.fail("a different entry borrowed the scoped writer")
            with pytest.raises(CustodyUnavailable):
                async with ordinary.transaction(_local_sqlite_writer=entry):
                    pytest.fail("an ordinary database accepted a scoped writer entry")
            with pytest.raises(CustodyUnavailable):
                await database.run(LocalSQLiteWriterUnit(other_entry, token_id))
            with pytest.raises(CustodyUnavailable):
                _ = database.writer_engine
            with pytest.raises(CustodyUnavailable):
                _ = database.reader_engine
            with pytest.raises(CustodyUnavailable):
                async with database.migration_connection():
                    pytest.fail("a scoped instance opened a migration checkout")
            with pytest.raises(CustodyUnavailable):
                await attempt_unqualified(other_settings, participant)
            with pytest.raises(CustodyUnavailable):
                await attempt_unqualified(settings, missing)
            with pytest.raises(DatabaseWriterRejected):
                async with database.transaction(
                    admission_deadline=asyncio.get_running_loop().time() - 1.0,
                    _local_sqlite_writer=entry,
                ):
                    pytest.fail("an expired caller deadline borrowed the scoped writer")
            await priority.request()
            try:
                with pytest.raises(CustodyUnavailable):
                    await database.run(LocalSQLiteWriterUnit(entry, token_id))
            finally:
                await priority.withdraw()
        finally:
            await ordinary.close()
            await other_database.close()
            await database.close()

    assert pool.opened == pool.checked_out == pool.checked_in == pool.closed == []
    assert {"root", "lifetime", "participant"}.isdisjoint(dir(entry))
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert read_metadata(metadata) == original
    assert read_pending(root) is None
    assert_released(root, metadata)


def journal_mode(path: Path) -> object:
    with (
        sqlite_contender(path, readonly=True) as observer,
        closing(observer.execute("PRAGMA journal_mode")) as cursor,
    ):
        result: object = cursor.fetchone()
        return result


@pytest.mark.asyncio
async def test_custody_original_writer_refuses_native_only_authority_alias(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)
    with (
        closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
        ) as root,
        closing(CustodyRecoveryPriority(root)) as coordinator,
        closing(CustodyLifetimeLease(root)) as lifetime,
    ):
        await initialize(root)
        await coordinator.register(journal_id=JOURNAL_ID, generation=GENERATION)
        participant = await ordinary_registration(root, lifetime)
        journal_pathname = root.path / "authority.sqlite3"
        settings = sqlite_settings(journal_pathname)
        original_authority = read_authority(root.path)
        assert root.read_latch() is CustodyLatch.OPEN
        assert journal_mode(journal_pathname) == ("delete",)

        async def write() -> None:
            database, entry = local_sqlite_writer(settings, root, lifetime, participant)
            try:
                async with database.transaction(_local_sqlite_writer=entry):
                    pytest.fail("the native-only journal was lent as metadata")
            finally:
                await database.close()

        with observe_pool() as pool, pytest.raises(CustodyUnavailable):
            await write()

        assert pool.opened == pool.checked_out == pool.checked_in == pool.closed == []
        assert journal_mode(journal_pathname) == ("delete",)
        assert read_authority(root.path) == original_authority
        assert read_pending(root) is None
        assert not (root.path / "authority.sqlite3-wal").exists()
        assert not (root.path / "authority.sqlite3-shm").exists()
        with root.acquire("commit.lock"), root.acquire("journal-order.lock"):
            assert root.read_latch() is CustodyLatch.OPEN


@pytest.mark.asyncio
async def test_custody_timed_begin_busy_refuses_without_replacing_writer(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    first, second, _ = owners.tokens
    loop = asyncio.get_running_loop()

    with observe_pool() as pool:
        try:
            assert await database.run(LocalSQLiteWriterUnit(entry, first)) == 1
            original = read_metadata(metadata)
            with sqlite_contender(metadata) as competing:
                with closing(competing.execute("BEGIN IMMEDIATE")):
                    pass
                with pytest.raises(DatabaseWriterRejected) as refused:
                    async with database.transaction(
                        _local_sqlite_writer=entry,
                        admission_deadline=loop.time() + CONDITION_DEADLINE_SECONDS,
                    ):
                        pytest.fail("a busy SQLite original writer was lent")
                after_refusal = read_metadata(metadata)
                assert competing.in_transaction
                assert read_pending(owners.root) is None
                assert database.writer_is_free()
                assert database.units_in_flight() == 0
                competing.rollback()
            async with database.transaction(
                _local_sqlite_writer=entry,
                admission_deadline=loop.time() + CONDITION_DEADLINE_SECONDS,
            ) as recovered:
                count = await recovered.revoke_token(second)
            after_recovery = read_metadata(metadata)
        finally:
            await database.close()

    assert refused.value.reason is DatabaseWriterFailure.UNAVAILABLE
    assert after_refusal == original
    assert after_recovery.previous == original.frontier
    assert after_recovery.frontier.sequence == original.frontier.sequence + 1
    assert after_recovery.revoked == (True, True, False)
    assert count == 1
    assert len(pool.opened) == 1
    assert pool.checked_out == pool.checked_in == pool.opened * 3
    assert pool.closed == pool.opened
    assert_released(owners.root, metadata)


@pytest.mark.asyncio
async def test_custody_writer_shutdown_joins_admission_before_checkout(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)

    async def write() -> None:
        async with database.transaction(_local_sqlite_writer=entry):
            pytest.fail("the closed database lent an admitted writer")

    with observe_writer(WriterCut.ADMISSION) as native, observe_pool() as pool:
        task = asyncio.create_task(write())
        try:
            await until(native.entered.is_set)
            with (
                pytest.raises(BlockingIOError),
                owners.root.acquire("journal-order.lock"),
            ):
                pytest.fail("the admission phase released J with work outstanding")
            assert database.units_in_flight() == 1
            assert pool.checked_out == []
            await database.close(deadline_seconds=0)
            assert database.lifecycle is Lifecycle.CLOSED
            assert database.units_in_flight() == 1
            assert not task.done()
            assert_commit_lock_owned(owners.root)
            native.release.set()
            with pytest.raises(CustodyUnavailable):
                await task
        finally:
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()

    assert native.statements == []
    assert pool.opened == pool.checked_out == pool.checked_in == pool.closed == []
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert read_metadata(metadata) == original
    assert read_pending(owners.root) is None
    assert_released(owners.root, metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True], ids=["cancel", "close"])
async def test_custody_writer_joins_cold_connection_callback(
    writer_owners: WriterOwners, shutdown: bool
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)
    entered = asyncio.Event()
    release = asyncio.Event()
    callback_finished = asyncio.Event()
    native_scope: list[str | None] = []

    def observe_scope(
        action: int,
        name: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_SAVEPOINT or (
            action == sqlite3.SQLITE_TRANSACTION and name == "BEGIN"
        ):
            native_scope.append(name)
        return sqlite3.SQLITE_OK

    def hold_callback(connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        driver: object = record.driver_connection
        assert isinstance(driver, SqliteDriverConnection)
        await_(driver.set_authorizer(observe_scope))
        entered.set()
        await_(release.wait())
        callback_finished.set()

    async def write() -> None:
        async with database.transaction(_local_sqlite_writer=entry):
            pytest.fail("the interrupted cold checkout was lent")

    cut = WriterCut.NATIVE_CLOSE if shutdown else None
    with observe_writer(cut) as native, observe_pool() as pool:
        event.listen(Pool, "connect", hold_callback)
        task = asyncio.create_task(write())
        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            if shutdown:
                await database.close(deadline_seconds=0)
                assert database.lifecycle is Lifecycle.CLOSED
            else:
                await deliver_cancellation(task)
            assert not task.done()
            assert not callback_finished.is_set()
            assert_commit_lock_owned(owners.root)
            assert database.units_in_flight() == 1
            assert not database.writer_is_free()
            assert len(pool.opened) == 1
            assert pool.checked_out == pool.closed == []
            release.set()
            if shutdown:
                await until(lambda: native.entered.is_set() or task.done())
                assert native.entered.is_set()
                assert native.closed_connections == []
                assert_commit_lock_owned(owners.root)
                assert database.units_in_flight() == 1
                assert not task.done()
                native.release.set()
            expected = CustodyUnavailable if shutdown else asyncio.CancelledError
            with pytest.raises(expected):
                await task
            if shutdown:
                assert len(native.closed_connections) == 1
                assert_native_closed(native.closed_connections[0])
            assert_released(owners.root, metadata)
        finally:
            release.set()
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(Pool, "connect", hold_callback)
            await database.close()

    assert callback_finished.is_set()
    assert native_scope == []
    assert native.statements == []
    assert len(native.closed_connections) == 1
    assert pool.opened == pool.closed
    assert len(pool.checked_out) == len(pool.checked_in) <= 1
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert read_metadata(metadata) == original
    assert read_pending(owners.root) is None


@pytest.mark.asyncio
async def test_custody_writer_retires_driver_after_failed_cold_configuration(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    anchor = file_anchor(metadata.stat(), metadata)
    malformed = b"invalid SQLite fixture header".ljust(4096, b"\0")
    metadata.write_bytes(malformed)
    original_authority = read_authority(owners.root.path)
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )

    async def write() -> None:
        async with database.transaction(_local_sqlite_writer=entry):
            pytest.fail("damaged metadata was lent as an original writer")

    with observe_writer(WriterCut.NATIVE_CLOSE) as native, observe_pool() as pool:
        task = asyncio.create_task(write())
        try:
            await until(lambda: native.entered.is_set() or task.done())
            assert native.entered.is_set()
            assert pool.opened == pool.checked_out == []
            assert native.closed_connections == []
            assert_commit_lock_owned(owners.root)
            assert database.units_in_flight() == 1
            assert not task.done()
            native.release.set()
            with pytest.raises(DBAPIError) as refused:
                await task
            assert len(native.closed_connections) == 1
            assert native.closed_connections[0].acquired_main()
            assert_native_closed(native.closed_connections[0])
        finally:
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()

    failure: object = refused.value.orig
    assert isinstance(failure, sqlite3.DatabaseError)
    assert failure.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert file_anchor(metadata.stat(), metadata) == anchor
    assert metadata.read_bytes() == malformed
    assert pool.opened == pool.checked_out == pool.checked_in == []
    assert native.statements == []
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert read_authority(owners.root.path) == original_authority
    assert read_pending(owners.root) is None
    with owners.root.acquire("commit.lock"), owners.root.acquire("journal-order.lock"):
        assert owners.root.read_latch() is CustodyLatch.OPEN


@pytest.mark.asyncio
async def test_custody_cold_main_refusal_settles_original_after_incumbent_close(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    identity = native_identity(metadata)
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    before = read_metadata(metadata)
    with (
        sqlite_contender(metadata) as incumbent,
        observe_writer(WriterCut.COLD_MAIN_ADMISSION) as observed,
    ):
        incumbent.execute("BEGIN IMMEDIATE").close()
        original_fds = held_main_descriptors(identity)
        task = asyncio.create_task(
            database.run(LocalSQLiteWriterUnit(entry, owners.tokens[0]))
        )
        try:
            await until(observed.entered.is_set)
            attempted_fds = held_main_descriptors(identity)
            assert len(original_fds) == 1
            assert len(attempted_fds) == 2
            assert set(original_fds) < set(attempted_fds)
            metadata.chmod(0o640)
            observed.release.set()
            with pytest.raises(CustodyUnavailable):
                await task
            metadata.chmod(0o600)
            assert held_main_descriptors(identity) == attempted_fds
            assert_commit_lock_owned(owners.root)
            assert database.units_in_flight() == 1
            assert not observed.statements
            incumbent.commit()
            await asyncio.to_thread(incumbent.close)
            await until(lambda: database.units_in_flight() == 0)
            assert not held_main_descriptors(identity)
            assert read_metadata(metadata) == before
            assert read_pending(owners.root) is None
            assert_released(owners.root, metadata)
        finally:
            metadata.chmod(0o600)
            observed.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()


@dataclass
class ColdConnectFailure:
    loop: asyncio.AbstractEventLoop
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    joining: asyncio.Event = field(default_factory=asyncio.Event)
    release: threading.Event = field(default_factory=threading.Event)
    workers: list[threading.Thread] = field(default_factory=list[threading.Thread])
    descriptors: list[int] = field(default_factory=list[int])
    armed: bool = False

    def observe(self, frame: FrameType, event: str, operation: object) -> None:
        code = frame.f_code
        if (
            code is open_sqlite_file.__code__
            and frame.f_locals.get("custody") is not None
        ):
            if event == "call" and self.armed:
                self.armed = False
                self.workers.append(threading.current_thread())
                self.exhaust_descriptors()
            elif event == "return" and self.is_original_worker():
                self.release_descriptors()
        elif (
            event == "call"
            and frame.f_globals is aiosqlite.core.__dict__
            and code.co_name == "close_and_stop"
            and self.is_original_worker()
        ):
            self.loop.call_soon_threadsafe(self.stopping.set)
            if not self.release.wait(CONDITION_DEADLINE_SECONDS):
                raise TimeoutError("the failed cold worker was not released")
        elif (
            event == "call"
            and code is threading.Thread.join.__code__
            and self.workers
            and frame.f_locals.get("self") is self.workers[0]
        ):
            self.loop.call_soon_threadsafe(self.joining.set)

    def is_original_worker(self) -> bool:
        return bool(self.workers) and threading.current_thread() is self.workers[0]

    def exhaust_descriptors(self) -> None:
        for _ in range(64):
            try:
                self.descriptors.append(os.open(os.devnull, os.O_RDONLY))
            except OSError as error:
                if error.errno == errno.EMFILE:
                    return
                self.release_descriptors()
                raise
        self.release_descriptors()
        raise AssertionError("the fixture did not exhaust its descriptor allowance")

    def release_descriptors(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


async def exercise_cold_connect_failure(tmp_path: Path, sqlite_template: Path) -> None:
    cut = ColdConnectFailure(asyncio.get_running_loop())
    previous = threading.getprofile()
    threading.setprofile(cut.observe)
    try:
        async with open_writer_owners(tmp_path, sqlite_template) as owners:
            await assert_cold_connect_failure_retires_original(owners, cut)
    finally:
        cut.release.set()
        threading.setprofile(previous)


async def assert_cold_connect_failure_retires_original(
    owners: WriterOwners, cut: ColdConnectFailure
) -> None:
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)
    authority_before = read_authority(owners.root.path)

    async def write() -> None:
        async with database.transaction(_local_sqlite_writer=entry):
            pytest.fail("the failed cold checkout was lent")

    with observe_pool() as pool:
        cut.armed = True
        writer = asyncio.create_task(write())
        joining = asyncio.create_task(cut.joining.wait())
        try:
            await asyncio.wait_for(cut.stopping.wait(), CONDITION_DEADLINE_SECONDS)
            await asyncio.wait(
                (writer, joining),
                timeout=CONDITION_DEADLINE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )

            assert len(cut.workers) == 1
            worker = cut.workers[0]
            assert worker.is_alive()
            assert_commit_lock_owned(owners.root)
            assert not writer.done(), "writer exited before its original worker stopped"
            assert joining.done(), "writer did not join its original worker"
            assert database.units_in_flight() == 1
            assert not database.writer_is_free()
            assert pool.opened == pool.checked_out == []
            assert read_metadata(metadata) == original
            assert read_authority(owners.root.path) == authority_before
            assert read_pending(owners.root) is None

            cut.release.set()
            with pytest.raises(DBAPIError) as refused:
                await writer
            failure: object = refused.value.orig
            assert isinstance(failure, sqlite3.OperationalError)
            assert failure.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN
            assert not worker.is_alive()
            assert database.units_in_flight() == 0
            assert database.writer_is_free()
            assert_released(owners.root, metadata)

            async with database.transaction(_local_sqlite_writer=entry) as loan:
                changed = await loan.revoke_token(owners.tokens[0])
            assert changed == 1
            assert len(pool.checked_out) == len(pool.checked_in) == 1
        finally:
            cut.release.set()
            if not joining.done():
                joining.cancel()
            await asyncio.gather(writer, joining, return_exceptions=True)
            for worker in cut.workers:
                await asyncio.to_thread(worker.join, CONDITION_DEADLINE_SECONDS)
            await database.close()


COLD_CONNECT_FAILURE_CHILD = """
import asyncio
import resource
import sys
from pathlib import Path

from tests.db.test_custody_sqlite_writer import exercise_cold_connect_failure

limit = resource.RLIMIT_NOFILE
resource.setrlimit(limit, (64, resource.getrlimit(limit)[1]))
asyncio.run(exercise_cold_connect_failure(Path(sys.argv[1]), Path(sys.argv[2])))
"""


def test_custody_writer_joins_failed_cold_connect_without_handle(
    tmp_path: Path, sqlite_template: Path
) -> None:
    command = [
        sys.executable,
        "-c",
        COLD_CONNECT_FAILURE_CHILD,
        str(tmp_path),
        str(sqlite_template),
    ]

    result = subprocess.run(command, capture_output=True, timeout=30, check=False)

    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.asyncio
async def test_custody_writer_unstarted_driver_retires_without_join(
    writer_owners: WriterOwners,
) -> None:
    driver = CustodyDriverConnection(
        lambda: pytest.fail("the unstarted writer was opened")
    )

    await driver.close()

    assert driver.retired


@pytest.mark.asyncio
@pytest.mark.parametrize("timed", [False, True], ids=["untimed", "timed"])
async def test_custody_writer_cancellation_before_checkout_dispatch(
    writer_owners: WriterOwners, timed: bool
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)
    authority_before = read_authority(owners.root.path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONDITION_DEADLINE_SECONDS if timed else None
    scheduled = False
    drivers = 0

    def observe(frame: FrameType, event: str, operation: object) -> None:
        nonlocal scheduled, drivers
        if event != "call":
            return
        name = frame.f_code.co_qualname
        if name == "Database._open_local_sqlite_checkout" and not scheduled:
            owner = asyncio.current_task()
            assert owner is not None
            scheduled = True
            loop.call_soon(owner.cancel)
        elif frame.f_code is SqliteDriverConnection.__init__.__code__:
            drivers += 1

    async def write() -> None:
        async with database.transaction(
            _local_sqlite_writer=entry, admission_deadline=deadline
        ):
            pytest.fail("the cancelled cold checkout was lent")

    previous = sys.getprofile()
    with observe_pool() as pool:
        sys.setprofile(observe)
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.create_task(write())
        finally:
            sys.setprofile(previous)
            await database.close()

    assert scheduled
    assert drivers == 0
    assert pool.opened == pool.checked_out == []
    assert database.units_in_flight() == 0
    assert_released(owners.root, metadata)
    assert read_metadata(metadata) == original
    assert read_authority(owners.root.path) == authority_before
    assert read_pending(owners.root) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catch_cancellation", [False, True], ids=["propagated", "caught-by-borrower"]
)
async def test_custody_writer_loan_revokes_before_finalization(
    writer_owners: WriterOwners, catch_cancellation: bool
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)
    first, second, _ = owners.tokens
    borrower: asyncio.Future[LocalSQLiteWriterLoan] = (
        asyncio.get_running_loop().create_future()
    )
    caught = asyncio.Event()

    async def mutate() -> None:
        async with database.transaction(_local_sqlite_writer=entry) as loan:
            borrower.set_result(loan)
            try:
                await loan.revoke_token(first)
            except asyncio.CancelledError:
                if not catch_cancellation:
                    raise
                caught.set()

    with observe_writer(WriterCut.MUTATION) as native, observe_pool() as pool:
        task = asyncio.create_task(mutate())
        try:
            await until(native.entered.is_set)
            loan = borrower.result()
            await deliver_cancellation(task)
            assert not task.done()
            assert_open_ownership(owners.root, metadata)
            assert pool.checked_in == []
            assert database.units_in_flight() == 1
            assert not database.writer_is_free()
            assert read_pending(owners.root) is None
            with pytest.raises(CustodyUnavailable):
                await loan.revoke_token(second)
            native.release.set()
            expected = (
                CustodyUnavailable if catch_cancellation else asyncio.CancelledError
            )
            with pytest.raises(expected):
                await task
            with pytest.raises(CustodyUnavailable):
                await loan.revoke_token(second)
            after = read_metadata(metadata)
            saved = read_authority(owners.root.path)
        finally:
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()

    assert caught.is_set() == catch_cancellation
    assert native.mutation_ids == [first]
    assert native.mutations_completed == [True]
    assert_native_commands(native, (*ORIGINAL_COMMIT[:3], "ROLLBACK"))
    assert after == original
    assert_confirmed(saved, original.frontier)
    assert saved["allocated_commit"] == FIRST_ALLOCATION - 1
    assert read_pending(owners.root) is None
    assert len(pool.checked_out) == len(pool.checked_in) == 1
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert_released(owners.root, metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cut", [WriterCut.PREPARING, WriterCut.PREPARED, WriterCut.FRONTIER_ADVANCED]
)
async def test_custody_sqlite_prepare_cancellation_preserves_original_owner(
    writer_owners: WriterOwners, cut: WriterCut
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)
    first_token = owners.tokens[0]

    async def write() -> None:
        async with database.transaction(_local_sqlite_writer=entry) as loan:
            assert await loan.revoke_token(first_token) == 1

    with observe_writer(cut) as native, observe_pool() as pool:
        task = asyncio.create_task(write())
        try:
            await until(native.entered.is_set)
            pending_during_cut = read_pending(owners.root)
            if cut is WriterCut.PREPARING:
                with (
                    pytest.raises(BlockingIOError),
                    owners.root.acquire("journal-order.lock"),
                ):
                    pytest.fail("preparation released its active journal phase")
            else:
                with owners.root.acquire("journal-order.lock"):
                    assert pending_during_cut is not None
                    assert pending_during_cut[:2] == ("prepared", FIRST_ALLOCATION)
                    assert (
                        custody_identity(pending_during_cut[2])
                        != original.frontier.token
                    )
            await deliver_cancellation(task)
            assert not task.done()
            assert_open_ownership(owners.root, metadata)
            assert read_metadata(metadata) == original
            assert database.units_in_flight() == 1
            assert not database.writer_is_free()
            assert pool.checked_in == []
            native.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            after = read_metadata(metadata)
            saved = read_authority(owners.root.path)
        finally:
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()

    if cut is WriterCut.PREPARING:
        assert pending_during_cut is None
    completed = 5 if cut is WriterCut.FRONTIER_ADVANCED else 3
    assert_native_commands(native, (*ORIGINAL_COMMIT[:completed], "ROLLBACK"))
    assert len(native.compacted) == 1
    compacted = native.compacted[0]
    assert compacted.previous == original.frontier
    if pending_during_cut is not None:
        assert pending_during_cut == (
            "prepared",
            compacted.frontier.sequence,
            compacted.frontier.token,
        )
    assert compacted.phase == "aborted"
    assert after == original
    assert_confirmed(saved, original.frontier)
    assert saved["allocated_commit"] == FIRST_ALLOCATION
    assert read_pending(owners.root) is None
    assert len(pool.checked_out) == len(pool.checked_in) == 1
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert_released(owners.root, metadata)


@pytest.mark.asyncio
async def test_custody_dispatch_cancellation_joins_original_commit(
    writer_owners: WriterOwners,
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)

    async def revoke() -> None:
        async with database.transaction(_local_sqlite_writer=entry) as loan:
            assert await loan.revoke_token(owners.tokens[0]) == 1

    with observe_writer(WriterCut.DISPATCHING) as native, observe_pool() as pool:
        task = asyncio.create_task(revoke())
        try:
            await until(native.entered.is_set)
            await deliver_cancellation(task)
            assert not task.done()
            assert_open_ownership(owners.root, metadata)
            assert not pool.checked_in
            native.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_native_commands(native, ORIGINAL_COMMIT)
            committed = read_metadata(metadata)
            assert committed.revoked == (True, False, False)
            assert_confirmed(read_authority(owners.root.path), committed.frontier)
            assert read_pending(owners.root) is None
            assert pool.checked_in == pool.checked_out
            assert_released(owners.root, metadata)
        finally:
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()


@pytest.mark.asyncio
async def test_custody_abort_settles_after_retained_dispatch_journal_closes(
    writer_owners: WriterOwners, sqlite_template: Path, tmp_path: Path
) -> None:
    owners = writer_owners
    root = owners.root
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    incumbent_path = tmp_path / "independent.sqlite"
    copy_sqlite_template(sqlite_template, incumbent_path)
    identity = native_identity(incumbent_path)
    sidecar = root.path / "authority.sqlite3-journal"
    database, entry = local_sqlite_writer(
        owners.settings, root, owners.lifetime, owners.participant
    )
    before = read_metadata(metadata)
    with (
        sqlite_contender(incumbent_path) as incumbent,
        observe_writer(WriterCut.FRONTIER_ADVANCED) as native,
        observe_pool() as pool,
    ):
        incumbent.execute("BEGIN IMMEDIATE").close()
        task = asyncio.create_task(
            database.run(LocalSQLiteWriterUnit(entry, owners.tokens[0]))
        )
        try:
            await until(native.entered.is_set)
            sidecar.hardlink_to(incumbent_path)
            native.release.set()
            with pytest.raises(CustodyUnavailable):
                await task
            assert pool.checked_in == pool.checked_out
            assert_native_commands(native, (*ORIGINAL_COMMIT[:5], "ROLLBACK"))
            assert database.units_in_flight() == 1
            assert_commit_lock_owned(root)
            with pytest.raises(BlockingIOError), root.acquire("journal-order.lock"):
                pytest.fail("exact J released before the retained native receipt")
            assert len(held_main_descriptors(identity)) == 2
            sidecar.unlink()
            saved = read_pending(root)
            assert saved is not None
            assert saved[:2] == ("prepared", FIRST_ALLOCATION)
            incumbent.commit()
            await asyncio.to_thread(incumbent.close)
            await until(lambda: database.units_in_flight() == 0)
            assert not held_main_descriptors(identity)
            assert read_metadata(metadata) == before
            assert read_pending(root) is None
            assert_confirmed(read_authority(root.path), before.frontier)
            assert_released(root, metadata)
        finally:
            sidecar.unlink(missing_ok=True)
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [WriterCut.RECORDING, WriterCut.COMPACTING])
async def test_custody_writer_keeps_original_lease_until_settled(
    writer_owners: WriterOwners, cut: WriterCut
) -> None:
    owners = writer_owners
    database, entry = local_sqlite_writer(
        owners.settings, owners.root, owners.lifetime, owners.participant
    )
    metadata = owners.settings.sqlite_path.resolve(strict=True)
    original = read_metadata(metadata)
    first_token = owners.tokens[0]

    async def direct_write() -> None:
        async with database.transaction(_local_sqlite_writer=entry) as loan:
            assert await loan.revoke_token(first_token) == 1

    with observe_writer(cut) as native, observe_pool() as pool:
        if cut is WriterCut.RECORDING:
            task = asyncio.create_task(direct_write())
            phase = "dispatching"
        else:
            task = asyncio.create_task(
                database.run(LocalSQLiteWriterUnit(entry, first_token))
            )
            phase = "committed"
        try:
            await until(native.entered.is_set)
            after = read_metadata(metadata)
            pending = read_pending(owners.root)
            before_confirmation = read_authority(owners.root.path)
            await deliver_cancellation(task)
            assert not task.done()
            assert_commit_lock_owned(owners.root)
            assert_native_commands(native, ORIGINAL_COMMIT)
            assert database.units_in_flight() == 1
            assert not database.writer_is_free()
            assert pool.checked_out == pool.checked_in == pool.opened
            try_metadata_writer(metadata)
            with captured(
                DB_COMPONENT,
                LoggingSettings(),
                "original-writer-close",
                FakeClock().now,
            ) as diagnostics:
                await database.close(deadline_seconds=0)
            close_events = [
                record
                for record in diagnostics.finish()
                if record["event"] == CLOSE_TIMED_OUT.name
            ]
            assert database.lifecycle is Lifecycle.CLOSED
            assert pool.closed == []
            assert database.units_in_flight() == 1
            assert_commit_lock_owned(owners.root)
            native.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            saved = read_authority(owners.root.path)
        finally:
            native.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await database.close()

    assert pending == (phase, FIRST_ALLOCATION, after.frontier.token)
    assert after.previous == original.frontier
    assert after.revoked == (True, False, False)
    assert_confirmed(before_confirmation, original.frontier)
    assert saved["allocated_commit"] == FIRST_ALLOCATION
    assert_confirmed(saved, after.frontier)
    assert read_pending(owners.root) is None
    assert read_metadata(metadata) == after
    assert native.mutation_ids == [first_token]
    assert len(close_events) == 1
    assert pool.closed == pool.opened
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert_released(owners.root, metadata)
