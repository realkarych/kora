import errno
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.runtime import (
    STATE_ROOT_LENGTH,
    LocalRuntime,
    LocalRuntimeConfiguration,
    default_state_root,
)
from kora.plugins.runtime.local.spawn import (
    default_socket_directory,
    open_socket_directory,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.runner.filesystem import (
    FilesystemError,
    filesystem_kind,
    open_local_directory,
    refused_filesystem,
)
from kora.runner.registry import SOCKET_PATH_LENGTH
from tests.support.local_runners import short_directory
from tests.support.local_runtime import (
    PATH_MARKER,
    ConstructedIdentityReader,
    open_runtime,
    prepared,
    private_directory,
    protocol_error,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]


def symlinked(tmp_path: Path) -> Path:
    target = private_directory(tmp_path, "target")
    link = tmp_path / f"{PATH_MARKER}-link"
    link.symlink_to(target)
    return link


def symlinked_ancestor(tmp_path: Path) -> Path:
    parent = private_directory(tmp_path, f"{PATH_MARKER}-parent")
    private_directory(parent, "child")
    link = tmp_path / f"{PATH_MARKER}-ancestor-link"
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


def missing_parent(tmp_path: Path) -> Path:
    return tmp_path / f"{PATH_MARKER}-missing" / "sockets"


async def refusal_of(
    target: str, path: Path, base: Path, ctx: ProtocolContext
) -> KoraProtocolError:
    if target == "runtime":
        return await protocol_error(open_runtime(path, ctx))
    root = state_root(base)
    return await protocol_error(open_runtime(root, ctx, socket_directory=path))


def assert_runtime_refusal(error: Exception, code: str = "state_root_unsafe") -> None:
    assert isinstance(error, KoraProtocolError)
    assert (error.code, error.retryable, error.details) == (code, False, {})
    assert PATH_MARKER not in str(error) + repr(error.args)
    assert error.__cause__ is None
    assert error.__context__ is None


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


def socket_refusal_reason(path: Path) -> str:
    try:
        open_socket_directory(path).close()
    except (FilesystemError, OSError) as error:
        return reason(error)
    pytest.fail("the unsafe socket directory was accepted", pytrace=False)


def reason(error: Exception) -> str:
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
@pytest.mark.parametrize("target", ["runtime", "socket_directory"])
@pytest.mark.parametrize("leg", LEGS)
async def test_state_root_and_socket_dir_refuse_unsafe_ownership(
    tmp_path: Path, kora_capture: Capture, leg: str, target: str
) -> None:
    with short_directory() as short:
        base = tmp_path if target == "runtime" else short
        legs = {**UNPREPARED, **PREPARED}
        if target == "socket_directory":
            legs["missing"] = missing_parent
        path = legs[leg](base)
        ctx = runtime_context(kora_capture)
        before = sorted(os.listdir(base))

        refused = await refusal_of(target, path, base, ctx)

        created = sorted(set(os.listdir(base)) - set(before))
        cause = None if target == "runtime" else socket_refusal_reason(path)
    if target == "runtime":
        assert created == []
        assert_runtime_refusal(refused)
    else:
        assert created == [f"{PATH_MARKER}-state"]
        assert cause in REASONS[leg]
        assert_runtime_refusal(refused, "socket_directory_unsafe")
    records = kora_capture.finish()
    assert all(PATH_MARKER not in str(record) for record in records)


@pytest.mark.asyncio
async def test_socket_directory_creates_only_its_last_component(
    kora_capture: Capture,
) -> None:
    ctx = runtime_context(kora_capture)
    with short_directory() as base:
        sockets = base / "sockets"
        overlong = base / ("x" * SOCKET_PATH_LENGTH)
        runtime = await open_runtime(state_root(base), ctx, socket_directory=sockets)
        await runtime.close()
        refused = await protocol_error(
            open_runtime(base / f"{PATH_MARKER}-state", ctx, socket_directory=overlong)
        )
        mode = stat.S_IMODE(sockets.stat().st_mode)
        overlong_exists = overlong.exists()

    assert mode == 0o700
    assert not overlong_exists
    assert_runtime_refusal(refused, "socket_directory_unsafe")


@pytest.mark.asyncio
async def test_default_socket_directory_opens(
    tmp_path: Path, kora_capture: Capture
) -> None:
    runtime = await open_runtime(state_root(tmp_path), runtime_context(kora_capture))
    await runtime.close()

    default = default_socket_directory(sys.platform, os.getuid())
    metadata = os.lstat(default)
    assert stat.S_ISDIR(metadata.st_mode)
    assert (metadata.st_uid, stat.S_IMODE(metadata.st_mode)) == (os.getuid(), 0o700)
    assert default.resolve() == default


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


@pytest.mark.parametrize(
    "state_root",
    [
        f"/{PATH_MARKER}" + "x" * (STATE_ROOT_LENGTH - len(PATH_MARKER)),
        f"/{PATH_MARKER}\tstate",
        f"/{PATH_MARKER}\x00state",
        f"/{PATH_MARKER}\udcffstate",
    ],
    ids=["too_long", "tab", "nul", "surrogate"],
)
def test_state_root_configuration_refuses_unusable_text(state_root: str) -> None:
    with pytest.raises(ValidationError) as refused:
        LocalRuntimeConfiguration(state_root=state_root)

    assert PATH_MARKER not in str(refused.value)


def test_state_root_configuration_accepts_the_longest_root() -> None:
    longest = "/" + "x" * (STATE_ROOT_LENGTH - 1)

    configuration = LocalRuntimeConfiguration(state_root=longest)

    assert configuration.state_root == longest


@pytest.mark.parametrize(
    ("platform", "environment", "expected"),
    [
        ("linux", {"XDG_STATE_HOME": "/xdg/state"}, "/xdg/state/kora/local"),
        (
            "linux",
            {"XDG_STATE_HOME": "relative/state"},
            "/home/u/.local/state/kora/local",
        ),
        ("linux", {}, "/home/u/.local/state/kora/local"),
        (
            "darwin",
            {"XDG_STATE_HOME": "/xdg/state"},
            "/home/u/Library/Application Support/kora/local",
        ),
    ],
    ids=["xdg_absolute", "xdg_relative", "xdg_unset", "macos"],
)
def test_default_state_root_follows_the_platform(
    platform: str, environment: dict[str, str], expected: str
) -> None:
    root = default_state_root(platform, environment, Path("/home/u"))

    assert root == Path(expected)


def prepared_home(tmp_path: Path) -> Path:
    home = private_directory(tmp_path, f"{PATH_MARKER}-home")
    default_state_root(sys.platform, {}, home).mkdir(mode=0o700, parents=True)
    return home


def homeless() -> Path:
    raise RuntimeError("no home directory")


@pytest.mark.asyncio
async def test_runtime_opens_the_default_state_root(
    tmp_path: Path, kora_capture: Capture
) -> None:
    home = prepared_home(tmp_path)

    runtime = await LocalRuntime.open(
        LocalRuntimeConfiguration(state_root=None),
        runtime_context(kora_capture),
        identities=ConstructedIdentityReader(),
        environment={},
        home=lambda: home,
    )
    await runtime.close()

    assert (default_state_root(sys.platform, {}, home) / "domain.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "home",
    [
        homeless,
        lambda: Path(f"/{PATH_MARKER}" + "x" * STATE_ROOT_LENGTH),
        lambda: Path(f"/{PATH_MARKER}\tstate"),
    ],
    ids=["missing", "too_long", "tab"],
)
async def test_runtime_refuses_an_unusable_default_state_root(
    kora_capture: Capture, home: Callable[[], Path]
) -> None:
    refused = await protocol_error(
        LocalRuntime.open(
            LocalRuntimeConfiguration(state_root=None),
            runtime_context(kora_capture),
            identities=ConstructedIdentityReader(),
            environment={},
            home=home,
        )
    )

    assert_runtime_refusal(refused)
