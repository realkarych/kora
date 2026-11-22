import asyncio
import ctypes
import errno
import os
import platform
import socket
import sqlite3
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Protocol, cast

import pytest

from kora.api._linux_serving_barrier import (
    LinuxServingBarrierError,
    LinuxServingBarrierRefusal,
    install_linux_serving_barrier,
    linux_serving_filter_instructions,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.gate import ExecutionConfig, execute_workload, receive_execution

_CHILD_MODULE = "tests.api.test_linux_serving_barrier"
_CHILD_LIMIT = 20
_EXECUTED = 41
_EXEC_REFUSED = 42
_UNSUPPORTED = 78
_NATIVE_AUDIT_ARCH = 0xC000003E
_PERMISSION_REFUSAL = 0x00050001
_NOT_IMPLEMENTED = 0x00050026
_ALLOW = 0x7FFF0000
_PTHREAD_FLAGS = 0x003D0F00
_GATE_CLOCK = SystemClock()


class NativePrctl(Protocol):
    def __call__(
        self, operation: int, first: int, second: int, third: int, fourth: int, /
    ) -> int: ...


class NativeSystemCall(Protocol):
    def __call__(self, number: int, first: int, second: int, third: int, /) -> int: ...


def native_probe_required_here() -> bool:
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or sys.implementation.name != "cpython"
        or platform.python_version() != "3.11.17"
        or os.geteuid() == 0
    ):
        return False
    try:
        release = platform.freedesktop_os_release()
        libc_version = os.confstr("CS_GNU_LIBC_VERSION")
    except (OSError, ValueError):
        return False
    return (
        release.get("ID") == "ubuntu"
        and release.get("VERSION_ID") == "24.04"
        and libc_version == "glibc 2.39"
    )


def encoded_filter_action(
    instructions: bytes,
    *,
    arch: int,
    number: int,
    arguments: tuple[int, int, int] = (0, 0, 0),
) -> int:
    data = struct.pack("<IIQ3Q", number, arch, 0, *arguments)
    program = tuple(struct.iter_unpack("<HBBI", instructions))
    index = 0
    accumulator = 0
    for _ in program:
        code, when_true, when_false, value = program[index]
        match code:
            case 0x20:
                accumulator = struct.unpack_from("<I", data, value)[0]
                index += 1
            case 0x15:
                index += 1 + (when_true if accumulator == value else when_false)
            case 0x35:
                index += 1 + (when_true if accumulator >= value else when_false)
            case 0x45:
                index += 1 + (when_true if accumulator & value else when_false)
            case 0x06:
                return value
            case _:
                raise AssertionError("unrecognized seccomp instruction")
    raise AssertionError("seccomp program did not return")


def assert_encoded_fd_and_exec_policy() -> None:
    program = bytes(linux_serving_filter_instructions())
    cases = (
        (_NATIVE_AUDIT_ARCH, 57, (0, 0, 0), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 58, (0, 0, 0), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 59, (0, 0, 0), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 322, (0, 0, 0), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 435, (0, 0, 0), _NOT_IMPLEMENTED),
        (_NATIVE_AUDIT_ARCH, 56, (0, 0, 0), _PERMISSION_REFUSAL),
        (
            _NATIVE_AUDIT_ARCH,
            56,
            (_PTHREAD_FLAGS & ~0x400, 0, 0),
            _PERMISSION_REFUSAL,
        ),
        (
            _NATIVE_AUDIT_ARCH,
            56,
            (_PTHREAD_FLAGS | (1 << 32), 0, 0),
            _PERMISSION_REFUSAL,
        ),
        (_NATIVE_AUDIT_ARCH, 56, (_PTHREAD_FLAGS, 0, 0), _ALLOW),
        (_NATIVE_AUDIT_ARCH, 272, (0x400, 0, 0), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 436, (3, 3, 0x2), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 436, (3, 3, 0x4), _ALLOW),
        (_NATIVE_AUDIT_ARCH, 0x40000001, (0, 0, 0), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 512, (0, 0, 0), _PERMISSION_REFUSAL),
        (_NATIVE_AUDIT_ARCH, 547, (0, 0, 0), _PERMISSION_REFUSAL),
        (0x40000003, 4, (0, 0, 0), _PERMISSION_REFUSAL),
    )

    actions = tuple(
        encoded_filter_action(program, arch=arch, number=number, arguments=arguments)
        for arch, number, arguments, _ in cases
    )

    assert actions == tuple(expected for _, _, _, expected in cases)


def assert_native_fd_policy() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl.restype = ctypes.c_int
    read_prctl = cast(NativePrctl, prctl)
    syscall = libc.syscall
    syscall.argtypes = [ctypes.c_long] * 4
    syscall.restype = ctypes.c_long
    call = cast(NativeSystemCall, syscall)
    reader, writer = os.pipe()
    calls = ((272, 0x400, 0, 0), (436, writer, writer, 0x2), (435, 0, 0, 0))

    try:
        results = tuple((call(*request), ctypes.get_errno()) for request in calls)
        os.fstat(writer)
    finally:
        os.close(reader)
        os.close(writer)

    assert read_prctl(3, 0, 0, 0, 0) == 0
    assert read_prctl(39, 0, 0, 0, 0) == 1
    assert results == ((-1, errno.EPERM), (-1, errno.EPERM), (-1, errno.ENOSYS))


def held_thread_probe(ready: Event, release: Event) -> None:
    ready.set()
    assert release.wait(_CHILD_LIMIT)
    assert_native_fd_policy()


def sqlite_work(path: Path) -> object:
    assert_native_fd_policy()
    database = sqlite3.connect(path)
    try:
        database.execute("CREATE TABLE probe (value INTEGER)")
        database.execute("INSERT INTO probe (value) VALUES (42)")
        database.commit()
        result: object = database.execute("SELECT value FROM probe").fetchone()
        return result
    finally:
        database.close()


def attempt_fork() -> None:
    forked = os.fork()
    if forked == 0:
        raise SystemExit(94)
    os.waitpid(forked, 0)


def native_child(directory: Path) -> int:
    if not native_probe_required_here():
        with pytest.raises(LinuxServingBarrierError) as error:
            install_linux_serving_barrier()
        assert error.value.reason is LinuxServingBarrierRefusal.UNSUPPORTED_PROFILE
        return _UNSUPPORTED

    ready, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as existing_owner:
        earlier = existing_owner.submit(held_thread_probe, ready, release)
        try:
            assert ready.wait(_CHILD_LIMIT)
            install_linux_serving_barrier()
        finally:
            release.set()
        earlier.result(timeout=_CHILD_LIMIT)
        assert_encoded_fd_and_exec_policy()
    with pytest.raises(PermissionError) as refusal:
        attempt_fork()
    assert refusal.value.errno == errno.EPERM

    with ThreadPoolExecutor(max_workers=1) as later_owner:
        result = later_owner.submit(sqlite_work, directory / "thread.sqlite").result(
            timeout=_CHILD_LIMIT
        )

    assert result == (42,)
    return 0


def exec_child(mode: str, endpoint_fd: int, cwd_fd: int) -> int:
    if mode == "filtered":
        install_linux_serving_barrier()
    deadline = _GATE_CLOCK.monotonic_ns() + _CHILD_LIMIT * 1_000_000_000
    config = asyncio.run(
        receive_execution(socket.socket(fileno=endpoint_fd), cwd_fd, deadline)
    )

    try:
        execute_workload(config)
    except PermissionError as error:
        if error.errno != errno.EPERM:
            raise
        return _EXEC_REFUSED
    raise AssertionError("the fixed gate returned without executing")


async def activate_gate(endpoint: socket.socket) -> None:
    channel = BoundedChannel(endpoint, _GATE_CLOCK)
    deadline = _GATE_CLOCK.monotonic_ns() + _CHILD_LIMIT * 1_000_000_000
    config = ExecutionConfig(
        sys.executable, (sys.executable, "-I", "-c", "raise SystemExit(41)"), ()
    )
    metadata, payload = config.encode()

    try:
        await channel.write_frame(metadata, deadline)
        await channel.write_record(payload, deadline)
        assert await channel.read_frame(deadline) == b"ready"
        await channel.write_frame(b"activate", deadline)
    finally:
        channel.close()


async def gate_result(directory: Path, mode: str) -> int:
    parent_endpoint, child_endpoint = socket.socketpair()
    with parent_endpoint, child_endpoint:
        cwd_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    _CHILD_MODULE,
                    mode,
                    str(child_endpoint.fileno()),
                    str(cwd_fd),
                ],
                pass_fds=(child_endpoint.fileno(), cwd_fd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            os.close(cwd_fd)
        with child:
            child_endpoint.close()
            try:
                await activate_gate(parent_endpoint)
                stdout, stderr = child.communicate(timeout=_CHILD_LIMIT)
            finally:
                if child.poll() is None:
                    child.kill()
            assert stdout == b""
            assert stderr == b""
            return child.returncode


def test_linux_serving_barrier_denies_copy_and_preserves_threads(
    tmp_path: Path,
) -> None:
    expected = 0 if native_probe_required_here() else _UNSUPPORTED

    result = subprocess.run(
        [sys.executable, "-m", _CHILD_MODULE, "native", str(tmp_path)],
        capture_output=True,
        check=False,
        timeout=_CHILD_LIMIT,
    )

    assert result.returncode == expected, result.stderr.decode()
    assert result.stdout == b""
    assert result.stderr == b""
    assert_encoded_fd_and_exec_policy()


@pytest.mark.asyncio
async def test_linux_serving_barrier_reuses_owned_exec_gate(tmp_path: Path) -> None:
    needs_native_filter = native_probe_required_here()

    unfiltered = await gate_result(tmp_path, "unfiltered")
    filtered = await gate_result(tmp_path, "filtered") if needs_native_filter else None

    assert unfiltered == _EXECUTED
    assert filtered == (_EXEC_REFUSED if needs_native_filter else None)


if __name__ == "__main__":
    if sys.argv[1] == "native":
        raise SystemExit(native_child(Path(sys.argv[2])))
    raise SystemExit(exec_child(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
