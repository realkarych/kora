import json
import os
import re
import select
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.runner.filesystem import DirectoryAnchor, rename_no_replace
from kora.runner.identity import ArgumentMatch, IdentityState, NativeIdentityReader
from tests.support.local_runtime import (
    READY_DEADLINE_SECONDS,
    observer_namespace_text,
    open_runtime,
    prepared,
    private_directory,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]

EXEC_AFTER_MARK = """
import fcntl, os, sys
descriptor = int(sys.argv[1])
fcntl.fcntl(descriptor, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
os.execv(sys.argv[2], sys.argv[2:])
"""
FORKED_READ = """
import os, sys
from kora.runner.identity import NativeIdentityReader
reader = NativeIdentityReader()
child = os.fork()
if child == 0:
    sys.stdout.write(reader.read(os.getpid()).state.value + "\\n")
    sys.stdout.flush()
    os._exit(0)
os.waitpid(child, 0)
sys.stdout.write(reader.read(os.getpid()).state.value + "\\n")
"""


async def no_replace_rename(tmp_path: Path, capture: Capture) -> None:
    directory = private_directory(tmp_path, "rename")
    (directory / "first").write_bytes(b"first")
    (directory / "second").write_bytes(b"second")
    (directory / "third").write_bytes(b"third")
    anchor = DirectoryAnchor.open_path(directory)
    try:
        rename_no_replace(anchor, "first", "moved")
        with pytest.raises(FileExistsError):
            rename_no_replace(anchor, "second", "third")
    finally:
        anchor.close()

    assert (directory / "moved").read_bytes() == b"first"
    assert (directory / "second").read_bytes() == b"second"
    assert (directory / "third").read_bytes() == b"third"
    assert not (directory / "first").exists()


async def identity_fields(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    observation = NativeIdentityReader().read(os.getpid())
    runtime = await open_runtime(root, runtime_context(capture), NativeIdentityReader())
    await runtime.close()

    record = json.loads((root / "domain.json").read_bytes())
    process = observation.process
    assert process is not None
    identity = process.identity
    assert identity.pid == os.getpid()
    assert identity.pid_namespace == observer_namespace_text()
    if sys.platform == "linux":
        assert identity.host_id == Path("/etc/machine-id").read_text().strip()
        assert (
            identity.boot_id
            == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        )
        assert re.fullmatch(r"pid:\[[0-9]+\]", identity.pid_namespace or "")
    else:
        assert re.fullmatch(r"[0-9a-f]{32}", identity.host_id or "")
        assert identity.pid_namespace == identity.boot_id
    assert record["host_id"] == identity.host_id
    assert record["boots"] == [
        {"boot_id": identity.boot_id, "pid_namespace": identity.pid_namespace}
    ]


async def forked_reader_is_unknown(tmp_path: Path, capture: Capture) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", FORKED_READ],
        capture_output=True,
        check=False,
        timeout=READY_DEADLINE_SECONDS,
    )

    assert completed.returncode == 0
    assert completed.stdout.decode().splitlines() == ["unknown", "present"]


@contextmanager
def grouped(argv: list[str]) -> Generator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, process_group=0)
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], READY_DEADLINE_SECONDS)
        assert ready
        assert process.stdout.readline() == b"ready\n"
        yield process
    finally:
        process.kill()
        process.wait(timeout=READY_DEADLINE_SECONDS)
        if process.stdout is not None:
            process.stdout.close()


@contextmanager
def executed(*argv: str) -> Generator[subprocess.Popen[bytes]]:
    reader, writer = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", EXEC_AFTER_MARK, str(writer), *argv],
            pass_fds=(writer,),
            process_group=0,
        )
    finally:
        os.close(writer)
    try:
        ready, _, _ = select.select([reader], [], [], READY_DEADLINE_SECONDS)
        assert ready
        assert os.read(reader, 1) == b""
        yield process
    finally:
        os.close(reader)
        process.kill()
        process.wait(timeout=READY_DEADLINE_SECONDS)


def member_namespaces(process: subprocess.Popen[bytes]) -> list[tuple[str, str]]:
    group = NativeIdentityReader().group(process.pid)
    assert group.state is IdentityState.PRESENT
    return [
        (
            member.state.value,
            ""
            if member.process is None
            else member.process.identity.pid_namespace or "",
        )
        for _, member in group.members
    ]


async def other_uid_member(tmp_path: Path, capture: Capture) -> None:
    helper = prepared("setuid-sleep")

    with executed(str(helper), "30") as process:
        observed = NativeIdentityReader().read(process.pid)
        members = member_namespaces(process)

    if sys.platform == "linux":
        assert observed.state is IdentityState.PRESENT
        assert observed.process is not None
        assert observed.process.identity.pid_namespace == observer_namespace_text()
        assert members == [("present", observer_namespace_text())]
    else:
        assert observed.state is IdentityState.UNKNOWN
        assert members == [("unknown", "")]


async def own_vector_is_not_swept(tmp_path: Path, capture: Capture) -> None:
    reader = NativeIdentityReader()
    own = tuple(sys.orig_argv)

    carried = reader.carries(os.getpid(), own)
    swept = reader.processes_carrying(own, time.monotonic_ns() + 10**9)

    assert carried is ArgumentMatch.EQUAL
    assert swept is not None
    assert os.getpid() not in swept


async def empty_leading_argument_is_not_shifted(
    tmp_path: Path, capture: Capture
) -> None:
    code = "import os, signal\nos.write(1, b'r')\nsignal.pause()"
    reader = NativeIdentityReader()
    process = subprocess.Popen(
        ["/bin/bash", "-c", 'exec -a "" "$0" -I -c "$1"', sys.executable, code],
        env={"MARK": "1"},
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], READY_DEADLINE_SECONDS)
        assert ready
        assert process.stdout.read(1) == b"r"
        shifted = reader.carries(process.pid, ("-I", "-c", code, "MARK=1"))
        exact = reader.carries(process.pid, ("", "-I", "-c", code))
    finally:
        process.kill()
        process.wait(timeout=READY_DEADLINE_SECONDS)
        if process.stdout is not None:
            process.stdout.close()

    assert shifted is ArgumentMatch.DIFFERENT
    assert exact is ArgumentMatch.EQUAL


async def setuid_carrier_is_not_owned(tmp_path: Path, capture: Capture) -> None:
    helper = str(prepared("setuid-sleep"))

    with executed(helper, "30") as process:
        carried = NativeIdentityReader().carries(process.pid, (helper, "30"))

    assert carried is ArgumentMatch.DIFFERENT


async def child_namespace_member(tmp_path: Path, capture: Capture) -> None:
    prepared("user-namespaces")
    argv = [
        "unshare",
        "--user",
        "--map-root-user",
        "--pid",
        "--fork",
        "sh",
        "-c",
        "echo ready; exec sleep 30",
    ]

    with grouped(argv) as process:
        members = member_namespaces(process)

    assert len(members) == 2
    assert set(members) == {("present", observer_namespace_text())}


LEGS: dict[str, Callable[[Path, Capture], Awaitable[None]]] = {
    "no_replace_rename": no_replace_rename,
    "identity_fields": identity_fields,
    "forked_reader_is_unknown": forked_reader_is_unknown,
    "own_vector_is_not_swept": own_vector_is_not_swept,
    "empty_leading_argument_is_not_shifted": empty_leading_argument_is_not_shifted,
}
PREPARED_LEGS: dict[str, Callable[[Path, Capture], Awaitable[None]]] = {
    "other_uid_member": other_uid_member,
    "setuid_carrier_is_not_owned": setuid_carrier_is_not_owned,
    **(
        {"child_namespace_member": child_namespace_member}
        if sys.platform == "linux"
        else {}
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg",
    [
        *sorted(LEGS),
        *(
            pytest.param(name, marks=pytest.mark.host_fixture)
            for name in sorted(PREPARED_LEGS)
        ),
    ],
)
async def test_platform_mechanisms(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    await {**LEGS, **PREPARED_LEGS}[leg](tmp_path, kora_capture)
