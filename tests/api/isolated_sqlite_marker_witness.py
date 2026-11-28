import asyncio
import base64
import errno
import fcntl
import hashlib
import os
import select
import sqlite3
import subprocess
import sys
import zlib
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import IsolatedMetadataMain
from kora.db.custody_models import CustodyUnavailable
from kora.db.custody_schema import ISOLATED_FRONTIER_REVISION
from kora.db.custody_writer import CustodyDriverConnection
from kora.db.first_claim_basis import SQLITE_DDL as FIRST_CLAIM_DDL
from kora.db.run_event_page_v1 import SQLITE_EVENT_DDL
from kora.sqlite.driver import Connection
from kora.standalone.custody import IsolatedSchema
from tests.api.guarded_sqlite_prebind_child import (
    API_UID,
    BINDING_LIMIT,
    OTHER_UID,
    WITNESS_TIMEOUT,
    FileIdentity,
    LaunchBinding,
    WitnessCase,
    command,
    fixture_message,
    held_main_descriptors,
    native_identity,
    start_bound_child,
    unprivileged,
)

MODULE: str = "tests.api.guarded_sqlite_marker_child"
NATIVE_PAGE: int = 4096
PRIVATE_CASES: tuple[bytes, ...] = (b"KJT1", b"KMT1")
MIGRATION_CASES: frozenset[WitnessCase] = frozenset(
    (
        WitnessCase.MIGRATION,
        WitnessCase.MIGRATION_CANCELLED,
        WitnessCase.MIGRATION_PRE_CANCEL,
        WitnessCase.MIGRATION_BLOCKED,
        WitnessCase.MIGRATION_UNSETTLED,
        WitnessCase.MIGRATION_PARTIAL_SHM,
        WitnessCase.MIGRATION_UNMAP_UNKNOWN,
        WitnessCase.MIGRATION_ACQUIRE_FAILED,
        WitnessCase.MIGRATION_JOURNAL_UNKNOWN,
        WitnessCase.MIGRATION_JOURNAL_WITHDRAWN,
    )
)
MIGRATED_COPY_LIMIT: int = 2 * 1024 * 1024
MIGRATED_WIRE_LIMIT: int = 128 * 1024


@dataclass(frozen=True)
class ObservedMetadataMain(IsolatedMetadataMain):
    cancel: Callable[[], None] | None

    @staticmethod
    def canceller(case: WitnessCase) -> Callable[[], None] | None:
        if case is not WitnessCase.CANCELLED:
            return None
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()

        def cancel() -> None:
            loop.call_soon_threadsafe(task.cancel)

        return cancel

    def require_j_released(self) -> None:
        journal = self.path.parent / "custody-parent" / "custody" / "authority.sqlite3"
        assert not held_main_descriptors(native_identity(journal))
        order = journal.parent / "journal-order.lock"
        assert len(held_main_descriptors(native_identity(order))) == 1

    def open(self, attempt: Connection) -> Connection:
        self.require_j_released()
        return super().open(attempt)

    def mark(self, attempt: Connection) -> None:
        self.require_j_released()
        super().mark(attempt)
        if self.cancel is not None:
            assert not attempt.settled()
            assert len(held_main_descriptors(native_identity(self.path))) == 1
            self.cancel()


def cancel_on_original_loop() -> Callable[[], None]:
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()

    def request() -> None:
        delivered = Event()

        def cancel() -> None:
            task.cancel()
            loop.call_soon(delivered.set)

        loop.call_soon_threadsafe(cancel)
        assert delivered.wait(WITNESS_TIMEOUT)

    return request


def schema_canceller() -> Callable[[str], None]:
    cancel = cancel_on_original_loop()
    called = Event()

    def trace(statement: str) -> None:
        if "CREATE TABLE alembic_version" in statement and not called.is_set():
            called.set()
            cancel()
            barrier(b"MIGRATING")

    return trace


@dataclass(frozen=True)
class ObservedSchemaMain(ObservedMetadataMain):
    trace: Callable[[str], None] | None
    case: WitnessCase

    async def verify_migration_checkout(
        self,
        connection: AsyncConnection,
        original: CustodyDriverConnection,
        creator: Callable[[], Awaitable[CustodyDriverConnection]],
    ) -> None:
        await super().verify_migration_checkout(connection, original, creator)
        with pytest.raises(CustodyUnavailable):
            await creator()
        candidate = original.native_candidate
        assert candidate is not None
        with pytest.raises(sqlite3.ProgrammingError) as refusal:
            await original.custody_worker(
                lambda: candidate.checkpoint_isolated_migration(True)
            )
        assert refusal.value.sqlite_errorcode == sqlite3.SQLITE_MISUSE

    def mark(self, attempt: Connection) -> None:
        super().mark(attempt)
        if self.trace is not None:
            attempt.set_trace_callback(self.trace)
        if self.case is WitnessCase.MIGRATION_ACQUIRE_FAILED:
            stop_during_acquire(attempt)

    def observe_migration(self, descriptor: int, stage: int) -> bool:
        super().observe_migration(descriptor, stage)
        shm = self.path.with_name("main.db-shm")
        selected = native_identity(shm)
        if self.case is WitnessCase.MIGRATION_PARTIAL_SHM and stage == 1:
            assert held_main_descriptors(selected) == (descriptor,)
            assert not mapped(selected)
            barrier(b"SHM_ACQUIRED")
            return False
        if self.case is WitnessCase.MIGRATION_UNMAP_UNKNOWN and stage == 2:
            assert not held_main_descriptors(selected)
            assert not mapped(selected)
            return False
        return True


def stop_during_acquire(attempt: Connection) -> None:
    armed = Event()
    interrupted = Event()

    def trace(statement: str) -> None:
        if statement.strip().upper() == "PRAGMA MAIN.JOURNAL_MODE=WAL":
            armed.set()

    def progress() -> int:
        if not armed.is_set() or interrupted.is_set():
            return 0
        interrupted.set()
        barrier(b"ACQUIRE_INTERRUPTED")
        return 1

    attempt.set_trace_callback(trace)
    attempt.set_progress_handler(progress, 1)


def mapped(identity: FileIdentity) -> bool:
    expected_device, expected_inode = identity
    for line in Path("/proc/self/maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        major, minor = (int(piece, 16) for piece in fields[3].split(":"))
        if (
            os.makedev(major, minor) == expected_device
            and int(fields[4]) == expected_inode
        ):
            return True
    return False


def observe_schema(main: Path, journal: Path, case: WitnessCase) -> IsolatedSchema:
    sidecars = tuple(main.with_name(main.name + suffix) for suffix in ("-wal", "-shm"))
    captured: list[FileIdentity] = []
    held = (
        journal.parent / name
        for name in ("recovery.lock", "lifetime.lock", "commit.lock")
    )
    locks = tuple(held)
    cancel = (
        cancel_on_original_loop() if case is WitnessCase.MIGRATION_CANCELLED else None
    )

    def require_scope() -> None:
        assert all(
            len(held_main_descriptors(native_identity(path))) == 2 for path in locks
        )
        assert (journal.parent / "recovery.lock").read_bytes() == b"\x5a"
        assert not held_main_descriptors(native_identity(journal))

    def checkpoint_done() -> None:
        require_scope()
        captured.extend(native_identity(path) for path in (main, *sidecars))
        assert len(set(captured)) == 3
        assert all(len(held_main_descriptors(key)) == 1 for key in captured)
        assert mapped(captured[2])
        barrier(b"CHECKPOINT")

    def metadata_retired() -> None:
        require_scope()
        assert len(captured) == 3
        assert all(not held_main_descriptors(key) for key in captured)
        assert not mapped(captured[2])
        assert all(not path.exists() for path in sidecars)
        barrier(b"MAIN_RETIRED")

    def journal_retired() -> None:
        require_scope()
        barrier(b"JOURNAL_RETIRED")

    def require_active_journal() -> None:
        assert all(
            len(held_main_descriptors(native_identity(path))) == 2 for path in locks
        )
        assert len(held_main_descriptors(native_identity(journal))) == 1
        assert (
            len(
                held_main_descriptors(
                    native_identity(journal.parent / "journal-order.lock")
                )
            )
            == 2
        )

    def journal_dispatch() -> None:
        require_active_journal()
        if cancel is not None:
            cancel()
        barrier(b"JOURNAL_PENDING")

    def journal_committed() -> None:
        require_active_journal()
        barrier(b"JOURNAL_COMMITTED")
        if case is WitnessCase.MIGRATION_JOURNAL_UNKNOWN:
            raise CustodyUnavailable

    return IsolatedSchema(
        Path(__file__).with_name("isolated_sqlite_migrations"),
        checkpoint_done,
        metadata_retired,
        journal_dispatch,
        journal_committed,
        journal_retired,
    )


def assert_mountpoint_pinned(main: Path) -> None:
    for operation in (
        main.unlink,
        lambda: main.rename(main.with_name("unexpected.db")),
    ):
        try:
            operation()
        except OSError as error:
            if error.errno != errno.EBUSY:
                raise
        else:
            raise AssertionError("the original mountpoint could be removed")


def checkpoint(path: Path, marker: bytes) -> None:
    assert len(held_main_descriptors(native_identity(path))) == 1
    assert not any(
        path.with_name(path.name + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    )
    barrier(marker)


def barrier(marker: bytes) -> None:
    sys.stdout.buffer.write(marker + b"\n")
    sys.stdout.flush()
    assert select.select([sys.stdin.buffer], [], [], WITNESS_TIMEOUT)[0]
    assert sys.stdin.buffer.read(1) == b"1"


def copy_original(path: Path, target: Path, marker: bytes) -> None:
    with path.open("rb") as handle:
        source = handle.read(NATIVE_PAGE + 1)
    assert len(source) == NATIVE_PAGE
    assert source[68:72] == marker
    assert source[18:20] == b"\x01\x01"
    target.write_bytes(source)
    with closing(sqlite3.connect(f"{target.as_uri()}?immutable=1", uri=True)) as copy:
        assert copy.execute("SELECT count(*) FROM sqlite_schema").fetchone() == (0,)
    os.chown(target, API_UID, API_UID)
    fixture_message(
        f"MARKER {marker.decode()} sha256={hashlib.sha256(source).hexdigest()}"
    )


def check_refusal(
    binding: LaunchBinding, case: WitnessCase, main: Path, *, withdrawn: bool = False
) -> None:
    with start_bound_child(case, module=MODULE) as owned:
        if case is WitnessCase.FOREIGN:
            binding = binding.model_copy(update={"launcher": os.getppid()})
        elif case is WitnessCase.INEXACT and not withdrawn:
            binding = binding.model_copy(update={"main": (1, 1)})
        if withdrawn:
            owned.scope.close()
        if case is not WitnessCase.ABSENT:
            owned.startup.write(binding.model_dump_json().encode("ascii"))
        owned.startup.close()
        result, _ = owned.process.communicate(timeout=WITNESS_TIMEOUT)
        assert owned.process.returncode == 0
        assert result == b"REFUSED\n"
        assert main.stat().st_size == 0
        assert not (main.parent / "custody-parent" / "custody").exists()


def run_original(
    binding: LaunchBinding, backing: Path, main: Path, copies: Path, case: WitnessCase
) -> None:
    with start_bound_child(case, module=MODULE) as owned:
        child = owned.process
        assert child.stdin is not None
        assert child.stdout is not None
        owned.startup.write(binding.model_dump_json().encode("ascii"))
        owned.startup.close()
        journal = main.parent / "custody-parent" / "custody" / "authority.sqlite3"
        expected = ((b"KJT1", journal), (b"KMT1", backing))
        for marker, original in (
            expected[:1] if case is WitnessCase.CANCELLED else expected
        ):
            assert select.select([child.stdout], [], [], WITNESS_TIMEOUT)[0]
            assert child.stdout.readline(16) == marker + b"\n"
            command(*unprivileged(OTHER_UID, "refuse_access", f"/proc/{child.pid}/fd"))
            copy_original(original, copies / marker.decode(), marker)
            child.stdin.write(b"1")
            child.stdin.flush()
        if case in (
            WitnessCase.MIGRATION,
            WitnessCase.MIGRATION_CANCELLED,
            WitnessCase.MIGRATION_JOURNAL_UNKNOWN,
        ):
            if case is WitnessCase.MIGRATION_CANCELLED:
                expect_schema_barrier(child, b"MIGRATING")
            expect_schema_barrier(child, b"CHECKPOINT", journal, "schema_running")
            expect_schema_barrier(child, b"MAIN_RETIRED", journal, "schema_running")
            expect_schema_barrier(child, b"JOURNAL_PENDING", journal, "schema_running")
            expect_schema_barrier(
                child, b"JOURNAL_COMMITTED", journal, "schema_complete"
            )
            terminal = (
                b"JOURNAL_UNSETTLED"
                if case is WitnessCase.MIGRATION_JOURNAL_UNKNOWN
                else b"JOURNAL_RETIRED"
            )
            expect_schema_barrier(child, terminal, journal, "schema_complete")
            if case is WitnessCase.MIGRATION_JOURNAL_UNKNOWN:
                expect_schema_barrier(
                    child, b"RETAINED_AT_EXIT", journal, "schema_complete"
                )
        elif case is WitnessCase.MIGRATION_JOURNAL_WITHDRAWN:
            expect_schema_barrier(child, b"CHECKPOINT", journal, "schema_running")
            expect_schema_barrier(child, b"MAIN_RETIRED", journal, "schema_running")
            expect_schema_barrier(
                child,
                b"JOURNAL_PENDING",
                journal,
                "schema_running",
                observe=owned.scope.close,
            )
        elif case in (
            WitnessCase.MIGRATION_UNSETTLED,
            WitnessCase.MIGRATION_UNMAP_UNKNOWN,
        ):
            wal = main.with_name("main.db-wal")
            displaced = main.with_name("held-wal")
            expect_schema_barrier(
                child,
                b"CHECKPOINT",
                journal,
                "schema_running",
                observe=(lambda: wal.rename(displaced))
                if case is WitnessCase.MIGRATION_UNSETTLED
                else None,
            )
            expect_schema_barrier(child, b"UNSETTLED", journal, "schema_running")
            expect_schema_barrier(child, b"RETAINED_AT_EXIT", journal, "schema_running")
            if case is WitnessCase.MIGRATION_UNSETTLED:
                assert displaced.is_file()
                assert not wal.exists()
            else:
                assert wal.is_file()
                assert main.with_name("main.db-shm").is_file()
        elif case is WitnessCase.MIGRATION_PARTIAL_SHM:
            expect_schema_barrier(child, b"SHM_ACQUIRED", journal, "schema_running")
        elif case is WitnessCase.MIGRATION_ACQUIRE_FAILED:
            expect_schema_barrier(
                child, b"ACQUIRE_INTERRUPTED", journal, "schema_running"
            )
        result, _ = child.communicate(timeout=WITNESS_TIMEOUT)
        assert child.returncode == 0
        if case in MIGRATION_CASES:
            assert not Path(f"/proc/{child.pid}").exists()
            phase = (
                "schema_running"
                if case
                in (
                    WitnessCase.MIGRATION_BLOCKED,
                    WitnessCase.MIGRATION_UNSETTLED,
                    WitnessCase.MIGRATION_PRE_CANCEL,
                    WitnessCase.MIGRATION_PARTIAL_SHM,
                    WitnessCase.MIGRATION_UNMAP_UNKNOWN,
                    WitnessCase.MIGRATION_ACQUIRE_FAILED,
                    WitnessCase.MIGRATION_JOURNAL_WITHDRAWN,
                )
                else "schema_complete"
            )
            assert read_schema_phase(journal) == phase
            assert (
                result
                == {
                    WitnessCase.MIGRATION: b"SETTLED sequence=1\n",
                    WitnessCase.MIGRATION_CANCELLED: b"CANCELLED original_closed=1\n",
                    WitnessCase.MIGRATION_PRE_CANCEL: b"CANCELLED original_closed=1\n",
                    WitnessCase.MIGRATION_BLOCKED: (
                        b"MIGRATION_REFUSED original_closed=1\n"
                    ),
                    WitnessCase.MIGRATION_UNSETTLED: b"MIGRATION_UNSETTLED\n",
                    WitnessCase.MIGRATION_UNMAP_UNKNOWN: b"MIGRATION_UNSETTLED\n",
                    WitnessCase.MIGRATION_JOURNAL_UNKNOWN: b"MIGRATION_UNSETTLED\n",
                    WitnessCase.MIGRATION_PARTIAL_SHM: (
                        b"MIGRATION_REFUSED original_closed=1\n"
                    ),
                    WitnessCase.MIGRATION_ACQUIRE_FAILED: (
                        b"MIGRATION_REFUSED original_closed=1\n"
                    ),
                    WitnessCase.MIGRATION_JOURNAL_WITHDRAWN: (
                        b"MIGRATION_REFUSED original_closed=1\n"
                    ),
                }[case]
            )
            if case is WitnessCase.MIGRATION:
                export_migrated_copies(backing, journal, copies)
            fixture_message(
                f"SCHEMA_RESULT case={case.value} phase={phase} child_reaped=1"
            )
            return
        if case is WitnessCase.CANCELLED:
            assert result == b"CANCELLED original_closed=1\n"
            copy_original(backing, copies / "KMT1", b"KMT1")
            fixture_message(result.decode().strip())
            return
        assert result.startswith(b"SETTLED sequence=")
        assert len(result) < BINDING_LIMIT
        assert not Path(f"/proc/{child.pid}").exists()
        sequence = int(result.split(b"=", 1)[1])
        with closing(
            sqlite3.connect(f"{journal.as_uri()}?immutable=1", uri=True)
        ) as db:
            assert db.execute(
                "SELECT phase, alembic_revision, migration_connection_sequence, "
                "qualification_digest FROM provisioning"
            ).fetchone() == (
                "schema_running",
                ISOLATED_FRONTIER_REVISION,
                sequence,
                None,
            )
            assert db.execute(
                "SELECT format_version, qualification_digest, mode, "
                "confirmed_generation, confirmed_commit, confirmed_token FROM authority"
            ).fetchone() == (1073741826, None, "provisioning", 0, 0, bytes(16))
        fixture_message(result.decode().strip())
    for marker in PRIVATE_CASES:
        image = (copies / marker.decode()).read_bytes()
        assert len(image) == NATIVE_PAGE
        packed = base64.b64encode(zlib.compress(image)).decode("ascii")
        fixture_message(f"MARKER_COPY {marker.decode()} {packed}")


def read_schema_phase(journal: Path) -> str:
    with closing(sqlite3.connect(f"{journal.as_uri()}?immutable=1", uri=True)) as db:
        phase: object
        phase, digest, sequence = db.execute(
            "SELECT phase, qualification_digest, migration_connection_sequence "
            "FROM provisioning"
        ).fetchone()
        assert phase in ("schema_running", "schema_complete")
        assert digest is None
        assert sequence == 1
        assert isinstance(phase, str)
        assert db.execute(
            "SELECT mode, qualification_digest, confirmed_generation, confirmed_commit "
            "FROM authority"
        ).fetchone() == ("provisioning", None, 0, 0)
    return phase


def expect_schema_barrier(
    child: subprocess.Popen[bytes],
    request: bytes,
    journal: Path | None = None,
    phase: str = "",
    *,
    observe: Callable[[], object] | None = None,
) -> None:
    assert child.stdout is not None
    assert child.stdin is not None
    assert select.select([child.stdout], [], [], WITNESS_TIMEOUT)[0]
    assert child.stdout.readline(32) == request + b"\n"
    if journal is not None:
        assert read_schema_phase(journal) == phase
        assert (journal.parent / "recovery.lock").read_bytes() == b"\x5a"
        for name in (
            "recovery.lock",
            "lifetime.lock",
            "commit.lock",
            "journal-order.lock",
        ):
            with (journal.parent / name).open("rb") as lock:
                available = name == "journal-order.lock" and request not in (
                    b"JOURNAL_PENDING",
                    b"JOURNAL_COMMITTED",
                )
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    assert not available
                else:
                    assert available
    if observe is not None:
        observe()
    child.stdin.write(b"1")
    child.stdin.flush()


def export_migrated_copies(main: Path, journal: Path, copies: Path) -> None:
    for source, marker in ((main, "KMT1"), (journal, "KJT1")):
        with source.open("rb") as copied:
            image = copied.read(MIGRATED_COPY_LIMIT + 1)
        assert NATIVE_PAGE < len(image) <= MIGRATED_COPY_LIMIT
        assert image[68:72] == marker.encode()
        target = copies / f"migrated_{marker}"
        target.write_bytes(image)
        os.chown(target, API_UID, API_UID)
        fixture_message(
            f"SCHEMA_COPY {marker} bytes={len(image)} "
            f"sha256={hashlib.sha256(image).hexdigest()}"
        )
        if marker == "KMT1":
            with closing(
                sqlite3.connect(f"{target.as_uri()}?immutable=1", uri=True)
            ) as db:
                assert db.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone() == (ISOLATED_FRONTIER_REVISION,)
                assert db.execute(
                    "SELECT count(*) FROM custody_frontier"
                ).fetchone() == (0,)
                assert db.execute(
                    "SELECT sql FROM sqlite_schema WHERE "
                    "name IN ('ix_event_run_page_bounded', "
                    "'attempt_first_claim_basis') "
                    "ORDER BY name"
                ).fetchall() == [(FIRST_CLAIM_DDL,), (SQLITE_EVENT_DDL,)]
        assert target.read_bytes() == image
        packed = base64.b64encode(zlib.compress(image)).decode("ascii")
        assert len(packed) <= MIGRATED_WIRE_LIMIT
        for offset in range(0, len(packed), 512):
            sys.stderr.write(
                f"SCHEMA_WIRE {marker} {offset} {packed[offset : offset + 512]}\n"
            )
        sys.stderr.flush()


def test_installed_ordinary_open_refuses_migrated_private_main_copies(
    directory: Path, marker: str
) -> None:
    installed_copy_refusal(directory, marker, f"migrated_{marker}")


def test_installed_ordinary_open_refuses_real_marker_only_copies(
    directory: Path,
    marker: str,
) -> None:
    installed_copy_refusal(directory, marker, marker)


def installed_copy_refusal(
    directory: Path,
    marker: str,
    source: str,
) -> None:
    from tests.api.test_packaging import assert_installed_marker_refusal

    assert marker.encode("ascii") in PRIVATE_CASES
    target = directory / source
    scratch = directory / (source + "-ordinary")
    scratch.mkdir(mode=0o700)
    copy = scratch / "marker.sqlite"
    image = target.read_bytes()
    copy.write_bytes(image)
    with closing(Connection()) as checkout:
        try:
            checkout.open(copy, False, 0, lambda _fd, _empty: False)
        except sqlite3.DatabaseError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_NOTADB:
                raise
            assert checkout.acquired_main()
            assert checkout.settled()
        else:
            raise AssertionError("the installed driver accepted a marker copy")
    assert copy.read_bytes() == image
    assert_installed_marker_refusal(Path(sys.executable), scratch, copy, image)
    fixture_message(f"ORDINARY_REFUSED {marker} api=1 upgrade=1 provider=1")


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    for marker in PRIVATE_CASES:
        test_installed_ordinary_open_refuses_real_marker_only_copies(
            directory, marker.decode()
        )
