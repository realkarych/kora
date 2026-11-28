from __future__ import annotations

import asyncio
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

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
    MODULE,
    ObservedMetadataMain,
    assert_mountpoint_pinned,
    check_refusal,
    checkpoint,
    run_original,
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
    expected_heads = {"run_event_position_0035", ISOLATED_FRONTIER_REVISION}
    if set(graph.get_heads()) != expected_heads:
        raise CustodyUnavailable
    script = graph.get_revision(ISOLATED_FRONTIER_REVISION)
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
    return ISOLATED_FRONTIER_REVISION


def verify_writable(launch: ProtectedRead, parent: int) -> None:
    launch.require_launcher()
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

        try:
            sequence = await isolated_marker_checkpoints(
                ObservedMetadataMain(
                    launch.main,
                    file_anchor(info, launch.main),
                    parent,
                    require_live,
                    ObservedMetadataMain.canceller(case),
                ),
                isolated_head(),
                lambda: checkpoint(journal, b"KJT1"),
                lambda: checkpoint(launch.main, b"KMT1"),
            )
        except asyncio.CancelledError:
            if case is not WitnessCase.CANCELLED:
                raise
            sequence = None
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


if __name__ == "__main__":
    role, *words = sys.argv[1:]
    os.umask(0o077)
    if role == "child":
        initial, live, requested = words
        case = WitnessCase(requested)
        try:
            asyncio.run(run_marker_child(int(initial), int(live), case))
        except (CustodyUnavailable, WitnessScopeClosed):
            if case in (WitnessCase.SCHEMA, WitnessCase.CANCELLED):
                raise
            sys.stdout.write("REFUSED\n")
    elif role == "launcher":
        base, namespace, selected = words
        run_launcher(
            Path(base),
            int(namespace),
            lambda domain, device: (
                test_isolated_sqlite_marks_each_original_main_before_schema(
                    domain, device, WitnessCase(selected)
                )
            ),
        )
    elif role == "guest":
        if NAMESPACE_GATE.read_text().strip() != "0":
            raise CustodyUnavailable
        run_owned_guest(MODULE, WitnessCase(words[0]).value if words else "schema")
    else:
        raise ValueError("unknown isolated marker fixture role")
