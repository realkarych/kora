import errno
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.protocols.common import ProtocolContext
from kora.runner.filesystem import (
    FilesystemError,
    filesystem_kind,
    open_local_directory,
    refused_filesystem,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    open_registry,
    prepared,
    private_directory,
    runtime_context,
)

__all__ = ["kora_capture"]


def symlinked(tmp_path: Path) -> Path:
    target = private_directory(tmp_path, "target")
    link = tmp_path / f"{PATH_MARKER}-link"
    link.symlink_to(target)
    return link


def symlinked_ancestor(tmp_path: Path) -> Path:
    parent = private_directory(tmp_path, "parent")
    private_directory(parent, "child")
    link = tmp_path / "ancestor-link"
    link.symlink_to(parent)
    return link / "child"


def wide_mode(tmp_path: Path) -> Path:
    directory = private_directory(tmp_path, f"{PATH_MARKER}-wide")
    directory.chmod(0o750)
    return directory


def missing(tmp_path: Path) -> Path:
    return tmp_path / f"{PATH_MARKER}-missing"


def regular_file(tmp_path: Path) -> Path:
    path = tmp_path / f"{PATH_MARKER}-file"
    path.write_bytes(b"")
    path.chmod(0o700)
    return path


def foreign_owner(tmp_path: Path) -> Path:
    return prepared("foreign")


def foreign_ancestor(tmp_path: Path) -> Path:
    return prepared("foreign-parent") / "child"


def network_filesystem(tmp_path: Path) -> Path:
    return prepared("nfs-mount") / "state"


async def opened(target: str, path: Path, ctx: ProtocolContext) -> None:
    if target == "state_root":
        await (await open_registry(path, ctx)).close()
    else:
        open_local_directory(path).close()


UNPREPARED: dict[str, Callable[[Path], Path]] = {
    "symlink": symlinked,
    "symlink_ancestor": symlinked_ancestor,
    "wrong_mode": wide_mode,
    "missing": missing,
    "not_directory": regular_file,
}
PREPARED: dict[str, Callable[[Path], Path]] = {
    "foreign_owner": foreign_owner,
    "foreign_ancestor": foreign_ancestor,
    **({"network_filesystem": network_filesystem} if sys.platform == "linux" else {}),
}
SYMLINK_REFUSALS = frozenset({"ELOOP", "ENOTDIR"})
REASONS: dict[str, frozenset[str]] = {
    "symlink": SYMLINK_REFUSALS,
    "symlink_ancestor": SYMLINK_REFUSALS,
    "wrong_mode": frozenset({"private_identity_required"}),
    "missing": frozenset({"ENOENT"}),
    "not_directory": frozenset({"ENOTDIR"}),
    "foreign_owner": frozenset({"foreign_ancestor"}),
    "foreign_ancestor": frozenset({"foreign_ancestor"}),
    "network_filesystem": frozenset({"filesystem_refused"}),
}
LOCAL_FILESYSTEMS: frozenset[int | str] = frozenset(
    {0xEF53, 0x01021994, 0x58465342, 0x9123683E, "apfs", "hfs"}
)


def reason(error: BaseException) -> str:
    if isinstance(error, FilesystemError):
        return str(error.args[0])
    assert isinstance(error, OSError)
    assert error.errno is not None
    return errno.errorcode[error.errno]


LEGS = [
    *(pytest.param(name, id=name) for name in UNPREPARED),
    *(pytest.param(name, id=name, marks=pytest.mark.host_fixture) for name in PREPARED),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["state_root", "socket_directory"])
@pytest.mark.parametrize("leg", LEGS)
async def test_state_root_and_socket_dir_refuse_unsafe_ownership(
    tmp_path: Path, kora_capture: Capture, leg: str, target: str
) -> None:
    path = {**UNPREPARED, **PREPARED}[leg](tmp_path)
    ctx = runtime_context(kora_capture)
    before = sorted(os.listdir(tmp_path))

    with pytest.raises((FilesystemError, OSError)) as refused:
        await opened(target, path, ctx)

    assert sorted(os.listdir(tmp_path)) == before
    assert reason(refused.value) in REASONS[leg]
    records = kora_capture.finish()
    assert all(PATH_MARKER not in str(record) for record in records)


@pytest.mark.parametrize(
    "kind", [0x6969, 0x65735546, 0xFE534D42, "nfs", "smbfs", "macfuse"], ids=str
)
def test_refusal_table_refuses_network_and_user_space_filesystems(
    kind: int | str,
) -> None:
    refused = refused_filesystem(kind)

    assert refused


@pytest.mark.parametrize(
    "kind", [0xEF53, 0x58465342, 0x9123683E, 0x01021994, "apfs", "hfs"], ids=str
)
def test_refusal_table_accepts_local_filesystems(kind: int | str) -> None:
    refused = refused_filesystem(kind)

    assert not refused


def test_local_directory_reports_its_filesystem(tmp_path: Path) -> None:
    directory = private_directory(tmp_path, "local")

    anchor = open_local_directory(directory)
    try:
        kind = filesystem_kind(anchor.descriptor)
    finally:
        anchor.close()

    assert kind in LOCAL_FILESYSTEMS
    assert not refused_filesystem(kind)
