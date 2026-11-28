import asyncio
import base64
import errno
import hashlib
import os
import select
import sqlite3
import sys
import zlib
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from kora.db.connection import IsolatedMetadataMain
from kora.db.custody_schema import ISOLATED_FRONTIER_REVISION
from kora.sqlite.driver import Connection
from tests.api.guarded_sqlite_prebind_child import (
    API_UID,
    BINDING_LIMIT,
    OTHER_UID,
    WITNESS_TIMEOUT,
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
        result, _ = child.communicate(timeout=WITNESS_TIMEOUT)
        assert child.returncode == 0
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


def test_installed_ordinary_open_refuses_real_marker_only_copies(
    directory: Path,
    marker: str,
) -> None:
    from tests.api.test_packaging import assert_installed_marker_refusal

    assert marker.encode("ascii") in PRIVATE_CASES
    target = directory / marker
    scratch = directory / (marker + "-ordinary")
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
