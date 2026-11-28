from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint
from sqlalchemy.exc import DBAPIError

from kora.db.custody_models import CustodyUnavailable
from kora.db.custody_native import DIRECTORY_FLAGS, file_anchor
from kora.db.custody_schema import ISOLATED_FRONTIER_REVISION
from kora.db.migrations.environment import MIGRATIONS_DIRECTORY
from kora.standalone.custody import (
    isolated_marker_checkpoints,
    isolated_marker_owner_retained,
)
from tests.api.guarded_sqlite_prebind_child import (
    API_UID,
    OTHER_UID,
    LaunchBinding,
    ProtectedRead,
    WitnessCase,
    WitnessScopeClosed,
    boot_identity,
    command,
    consume_binding,
    fixture_message,
    held_main_descriptors,
    identity,
    mount_namespace,
    native_identity,
    open_launch_anchor,
    owned_mounts,
    process_start,
    protected_directories,
    run_launcher,
    run_owned_guest,
    unprivileged,
)
from tests.api.isolated_sqlite_marker_witness import (
    MIGRATION_CASES,
    MODULE,
    ObservedSchemaMain,
    assert_mountpoint_pinned,
    barrier,
    check_refusal,
    checkpoint,
    observe_schema,
    run_original,
    schema_canceller,
    test_installed_ordinary_open_refuses_migrated_private_main_copies,
)
from tests.api.isolated_sqlite_migrations.isolated_sqlite_frontier_v1 import (
    __file__ as PRIVATE_REVISION_PATH,
)
from tests.api.isolated_sqlite_migrations.isolated_sqlite_frontier_v1 import (
    frontier_elements,
)

NAMESPACE_GATE = Path("/proc/sys/kernel/unprivileged_userns_clone")
UNSETTLED_LAUNCH: list[ExitStack] = []


def isolated_head() -> str:
    private = Path(__file__).with_name("isolated_sqlite_migrations")
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
    config.set_main_option("path_separator", "newline")
    config.set_main_option(
        "version_locations", f"{MIGRATIONS_DIRECTORY / 'versions'}\n{private}"
    )
    graph = ScriptDirectory.from_config(config)
    head = graph.get_current_head()
    if head != ISOLATED_FRONTIER_REVISION:
        raise CustodyUnavailable
    script = graph.get_revision(head)
    if script.down_revision != "run_event_metadata_0034" or Path(script.path) != Path(
        PRIVATE_REVISION_PATH
    ):
        raise CustodyUnavailable
    versions = [
        str(element.sqltext)
        for element in frontier_elements()
        if isinstance(element, CheckConstraint)
        and element.name == "ck_custody_frontier_singleton_version"
    ]
    if versions != ["singleton = 1 AND format_version = 1073741825"]:
        raise CustodyUnavailable
    return head


def verify_writable(launch: ProtectedRead, parent: int) -> None:
    try:
        launch.require_launcher()
    except WitnessScopeClosed as error:
        raise CustodyUnavailable from error
    main = launch.main
    paths = protected_directories(launch.domain)
    parents = tuple(path.lstat() for path in paths)
    current = main.lstat()
    if (
        not NAMESPACE_GATE.read_text().strip() == "0"
        or tuple(map(identity, parents)) != launch.binding.ancestors
        or not all(
            stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            for info in parents[:-1]
        )
        or parents[-1].st_uid != API_UID
        or stat.S_IMODE(parents[-1].st_mode) != 0o700
        or identity(os.fstat(parent)) != identity(parents[-1])
        or owned_mounts(launch.domain, main) != launch.binding.mounts
        or identity(current) != launch.binding.main
        or not stat.S_ISREG(current.st_mode)
        or stat.S_IMODE(current.st_mode) != 0o600
        or current.st_uid != API_UID
        or current.st_nlink != 1
    ):
        raise CustodyUnavailable


async def run_marker_child(binding: int, lifetime: int, case: WitnessCase) -> None:
    with ExitStack() as owned:
        launch = owned.enter_context(consume_binding(binding, lifetime, case))
        parent = os.open(launch.main.parent, DIRECTORY_FLAGS)
        owned.callback(os.close, parent)

        def require_live() -> None:
            verify_writable(launch, parent)

        require_live()
        assert_mountpoint_pinned(launch.main)
        info = launch.main.lstat()
        assert info.st_size == 0
        journal = (
            launch.main.parent / "custody-parent" / "custody" / "authority.sqlite3"
        )
        sidecar = launch.main.with_name("main.db-shm")

        def metadata_checkpoint() -> None:
            checkpoint(launch.main, b"KMT1")
            if case is WitnessCase.MIGRATION_BLOCKED:
                sidecar.write_bytes(b"owned existing sidecar")
            if case is WitnessCase.MIGRATION_PRE_CANCEL:
                caller = asyncio.current_task()
                assert caller is not None
                caller.cancel()

        refused = False
        try:
            sequence = await isolated_marker_checkpoints(
                ObservedSchemaMain(
                    launch.main,
                    file_anchor(info, launch.main),
                    parent,
                    require_live,
                    ObservedSchemaMain.canceller(case),
                    schema_canceller()
                    if case is WitnessCase.MIGRATION_CANCELLED
                    else None,
                    case,
                ),
                isolated_head(),
                lambda: checkpoint(journal, b"KJT1"),
                metadata_checkpoint,
                schema=observe_schema(launch.main, journal, case)
                if case in MIGRATION_CASES
                else None,
            )
        except asyncio.CancelledError:
            if case not in (
                WitnessCase.CANCELLED,
                WitnessCase.MIGRATION_CANCELLED,
                WitnessCase.MIGRATION_PRE_CANCEL,
            ):
                raise
            sequence = None
        except (sqlite3.OperationalError, DBAPIError) as error:
            native = error.orig if isinstance(error, DBAPIError) else error
            code: object = getattr(native, "sqlite_errorcode", None)
            if case is not WitnessCase.MIGRATION_PARTIAL_SHM and (
                (case, code)
                not in (
                    (WitnessCase.MIGRATION_BLOCKED, sqlite3.SQLITE_CANTOPEN),
                    (WitnessCase.MIGRATION_ACQUIRE_FAILED, sqlite3.SQLITE_INTERRUPT),
                )
            ):
                raise
            if case is WitnessCase.MIGRATION_BLOCKED:
                assert sidecar.read_bytes() == b"owned existing sidecar"
            else:
                assert not sidecar.exists()
            assert not launch.main.with_name("main.db-wal").exists()
            refused = True
            sequence = None
        except CustodyUnavailable:
            if case is WitnessCase.MIGRATION_JOURNAL_WITHDRAWN:
                assert not sidecar.exists()
                assert not launch.main.with_name("main.db-wal").exists()
                assert not held_main_descriptors(launch.binding.main)
                assert not held_main_descriptors(native_identity(journal))
                assert not isolated_marker_owner_retained()
                sys.stdout.write("MIGRATION_REFUSED original_closed=1\n")
                return
            if case not in (
                WitnessCase.MIGRATION_UNSETTLED,
                WitnessCase.MIGRATION_UNMAP_UNKNOWN,
                WitnessCase.MIGRATION_JOURNAL_UNKNOWN,
            ):
                raise
            assert isolated_marker_owner_retained()
            assert not held_main_descriptors(launch.binding.main)
            wal_name = (
                "held-wal" if case is WitnessCase.MIGRATION_UNSETTLED else "main.db-wal"
            )
            wal = launch.main.with_name(wal_name)
            if case is WitnessCase.MIGRATION_JOURNAL_UNKNOWN:
                assert not wal.exists()
                assert not sidecar.exists()
            else:
                assert not held_main_descriptors(native_identity(wal))
            assert not held_main_descriptors(native_identity(journal))
            barrier(
                b"JOURNAL_UNSETTLED"
                if case is WitnessCase.MIGRATION_JOURNAL_UNKNOWN
                else b"UNSETTLED"
            )
            return
        finally:
            if isolated_marker_owner_retained():
                UNSETTLED_LAUNCH.append(owned.pop_all())
        assert not held_main_descriptors(native_identity(journal))
        assert not held_main_descriptors(launch.binding.main)
        assert (journal.parent / "recovery.lock").read_bytes() == b"\x5a"
        assert not isolated_marker_owner_retained()
        result = (
            f"SETTLED sequence={sequence}"
            if sequence
            else "MIGRATION_REFUSED original_closed=1"
            if refused
            else "CANCELLED original_closed=1"
        )
        sys.stdout.write(result + "\n")
        sys.stdout.flush()


def test_isolated_sqlite_marks_each_original_main_before_schema(
    domain: Path,
    device: str,
    case: WitnessCase,
) -> None:
    backing = domain / "backing" / "main.db"
    main = domain / "service" / "api" / "main.db"
    for path in (domain, main.parent.parent):
        path.mkdir(mode=0o750, exist_ok=True)
        os.chown(path, 0, API_UID)
        path.chmod(0o750)
    backing.parent.mkdir(mode=0o700)
    main.parent.mkdir(mode=0o700)
    backing.touch(mode=0o600, exist_ok=False)
    main.touch(mode=0o600, exist_ok=False)
    underlay_main = native_identity(main)
    with ExitStack() as held:
        underlay = open_launch_anchor(main)
        held.callback(os.close, underlay)
        for path in (
            main.parent,
            main.parent / "provider",
            main.parent / "custody-parent",
        ):
            path.mkdir(mode=0o700, exist_ok=True)
            os.chown(path, API_UID, API_UID)
        os.chown(backing, API_UID, API_UID)
        command("/usr/bin/mount", "--bind", str(backing), str(main))
        held.callback(command, "/usr/bin/umount", str(main))
        anchors = [
            open_launch_anchor(path)
            for path in (*protected_directories(domain), backing)
        ]
        for fd in anchors:
            held.callback(os.close, fd)
        facts = tuple(identity(os.fstat(fd)) for fd in anchors)
        assert identity(os.fstat(underlay)) != facts[-1]
        binding = LaunchBinding(
            launcher=os.getpid(),
            launcher_start=process_start(os.getpid()),
            boot=boot_identity(),
            namespace=mount_namespace(),
            mounts=owned_mounts(domain, main),
            directory=str(domain),
            ancestors=facts[:-1],
            main=facts[-1],
        )
        command(
            *unprivileged(
                OTHER_UID,
                "refuse_access",
                str(main.parent),
                str(main),
                str(backing),
                device,
                f"/proc/{os.getpid()}/fd/{underlay}",
            )
        )
        copies = domain.parent / "copies"
        copies.mkdir(mode=0o700)
        os.chown(copies, API_UID, API_UID)
        if case is WitnessCase.SCHEMA:
            for failure in (
                WitnessCase.ABSENT,
                WitnessCase.FOREIGN,
                WitnessCase.INEXACT,
            ):
                check_refusal(binding, failure, main)
            check_refusal(binding, WitnessCase.INEXACT, main, withdrawn=True)
        run_original(binding, backing, main, copies, case)
        assert tuple(identity(os.fstat(fd)) for fd in anchors) == facts
        assert owned_mounts(domain, main) == binding.mounts
    assert native_identity(main) == underlay_main
    assert main.stat().st_size == 0
    fixture_message("MARKER_RELEASE pin_released=1 original_child_reaped=1")


def test_isolated_sqlite_private_migration_publishes_schema_complete_after_release(
    domain: Path, device: str
) -> None:
    test_isolated_sqlite_marks_each_original_main_before_schema(
        domain, device, WitnessCase.MIGRATION
    )


def test_isolated_sqlite_private_migration_keeps_unsettled_original_fenced(
    domain: Path, device: str, case: WitnessCase
) -> None:
    assert case in MIGRATION_CASES.difference({WitnessCase.MIGRATION})
    test_isolated_sqlite_marks_each_original_main_before_schema(domain, device, case)


def run_case(domain: Path, device: str, case: WitnessCase) -> None:
    if case is WitnessCase.MIGRATION:
        test_isolated_sqlite_private_migration_publishes_schema_complete_after_release(
            domain, device
        )
    elif case in MIGRATION_CASES:
        test_isolated_sqlite_private_migration_keeps_unsettled_original_fenced(
            domain, device, case
        )
    else:
        test_isolated_sqlite_marks_each_original_main_before_schema(
            domain, device, case
        )


if __name__ == "__main__":
    role, *words = sys.argv[1:]
    os.umask(0o077)
    if role == "child":
        initial, live, requested = words
        case = WitnessCase(requested)
        try:
            asyncio.run(run_marker_child(int(initial), int(live), case))
        except (CustodyUnavailable, WitnessScopeClosed):
            if case in MIGRATION_CASES or case in (
                WitnessCase.SCHEMA,
                WitnessCase.CANCELLED,
            ):
                raise
            sys.stdout.write("REFUSED\n")
        else:
            if isolated_marker_owner_retained():
                barrier(b"RETAINED_AT_EXIT")
                sys.stdout.write("MIGRATION_UNSETTLED\n")
    elif role == "launcher":
        base, namespace, selected = words
        run_launcher(
            Path(base),
            int(namespace),
            lambda domain, device: run_case(domain, device, WitnessCase(selected)),
        )
    elif role == "copies":
        directory, marker = words
        test_installed_ordinary_open_refuses_migrated_private_main_copies(
            Path(directory), marker
        )
    elif role == "guest":
        if NAMESPACE_GATE.read_text().strip() != "0":
            raise CustodyUnavailable
        run_owned_guest(MODULE, WitnessCase(words[0]).value if words else "schema")
    else:
        raise ValueError("unknown isolated marker fixture role")
