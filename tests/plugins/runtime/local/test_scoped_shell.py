import asyncio
import ctypes
import fcntl
import hashlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
from array import array
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import utc_now
from kora.observability import Level, LoggingSettings
from kora.plugins.runtime.local.bound_checkout import BoundCheckout
from kora.plugins.runtime.local.command import COMPONENT
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.scoped_shell import (
    BASH_PATH,
    OUTPUT_BYTES,
    SCOPE_VARIABLE,
    SCOPED_SHELL,
    ScopedShell,
    ScopedShellAdmission,
    ScopedShellError,
    ScopedShellFailure,
    ScopedShellResult,
)
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.identity import CommandRead, IdentityState, NativeIdentityReader
from kora.runner.installed_executable import ExecutablePin, verify_installed_executable
from kora.runner.processes import start_gated
from tests.plugins.runtime.local.test_command import opaque_environment_process
from tests.support.local_peers import until
from tests.support.local_runners import short_directory
from tests.support.local_runtime import open_descriptors

WINDOW_NS: Final[int] = 12_000_000_000
ATTEMPT_MARKER: Final[str] = "scoped-shell-attempt-token"
PRIVATE_TEXT: Final[str] = "private-sensitive-split"
WRITE_PID: Final[str] = """
from pathlib import Path
def record(name, value):
    partial = Path(name + '.partial')
    partial.write_text(str(value))
    partial.replace(name)
"""
DESCENDANT: Final[str] = (
    WRITE_PID
    + """
import os
import signal
import time
record('leader', os.getpid())
if os.fork() == 0:
    if os.fork() != 0:
        os._exit(0)
    os.setsid()
    record('descendant', os.getpid())
    signal.pause()
    os._exit(0)
os.wait()
record('ready', 1)
while not Path('finish').exists():
    time.sleep(.01)
with open('executions', 'a') as counted:
    counted.write('ran')
print('finished')
"""
)


def python_command(script: str, *arguments: str) -> str:
    return "exec " + shlex.join((sys.executable, "-I", "-c", script, *arguments))


def before(clock: SystemClock) -> int:
    return clock.monotonic_ns() + WINDOW_NS


def admit(
    clock: SystemClock, command: str, current: Callable[[], bool] = lambda: True
) -> ScopedShellAdmission:
    return ScopedShellAdmission(command, new_operation_id(), before(clock), current)


@pytest.fixture
def shell_diagnostics() -> Generator[Capture]:
    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "scoped-shell", utc_now
    ) as capture:
        yield capture


@contextmanager
def subreaper() -> Generator[None]:
    if sys.platform != "linux":
        yield
        return
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@asynccontextmanager
async def shell_owner(
    workspace: Path,
    diagnostics: Capture,
    clock: SystemClock,
    siblings: Callable[[], tuple[ProcessRegistration, ...]] = tuple,
    *,
    altered_stamp: bool = False,
) -> AsyncGenerator[tuple[ScopedShell, BoundCheckout, AdoptedReaper]]:
    content = BASH_PATH.read_bytes()
    pin = ExecutablePin(len(content), hashlib.sha256(content).hexdigest())
    executable = verify_installed_executable(BASH_PATH, pin, before(clock), clock)
    if altered_stamp:
        executable = replace(executable, inode=executable.inode + 1)
    checkout = BoundCheckout.capture(workspace)
    reaper = AdoptedReaper(grace_ns=0, interval=0.02)
    with subreaper():
        reaper.start()
        owner = ScopedShell(
            checkout,
            executable,
            pin,
            home=workspace / "home",
            temporary=workspace / "tmp",
            attempt_token=diagnostics.scope.admit(ATTEMPT_MARKER),
            secrets=diagnostics.scope,
            clock=clock,
            reaper=reaper,
            siblings=siblings,
        )
        try:
            yield owner, checkout, reaper
        finally:
            try:
                assert await owner.settle(before(clock))
            finally:
                await reaper.stop()
                checkout.close()


def assert_gone(identity: ProcessIdentity) -> None:
    current = NativeIdentityReader().read(identity.pid)
    assert current.state is IdentityState.ABSENT or (
        current.process is not None and current.process.identity != identity
    )


def current_identity(path: Path) -> ProcessIdentity:
    observation = NativeIdentityReader().read(int(path.read_text()))
    assert observation.process is not None
    return observation.process.identity


def gate_descriptors() -> set[tuple[int, str]]:
    if sys.platform != "linux":
        return {(int(name), "") for name in os.listdir("/dev/fd")}
    descriptors: set[tuple[int, str]] = set()
    task_root = f"/proc/{os.getpid()}/task"
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except FileNotFoundError:
            continue
        if target == task_root or (
            target.startswith(f"{task_root}/") and target.endswith("/children")
        ):
            continue
        descriptors.add((int(name), target))
    return descriptors


@contextmanager
def prelaunch_process(reaper: AdoptedReaper) -> Generator[subprocess.Popen[bytes]]:
    with ExitStack() as fixture:
        with reaper.protect_spawn():
            process = fixture.enter_context(opaque_environment_process())
            reaper.register(process.pid)
        try:
            yield process
        finally:
            try:
                fixture.close()
                assert process.returncode == 0
            finally:
                reaper.release(process.pid)


@pytest.mark.asyncio
async def test_scoped_shell_keeps_registered_sibling_and_settles_descendants(
    tmp_path: Path,
    shell_diagnostics: Capture,
) -> None:
    workspace = tmp_path.resolve()
    clock = SystemClock()
    registrations: list[ProcessRegistration] = []
    async with shell_owner(
        workspace, shell_diagnostics, clock, lambda: tuple(registrations)
    ) as (
        owner,
        checkout,
        reaper,
    ):
        with prelaunch_process(reaper) as baseline:
            if sys.platform == "darwin":
                _member, state = NativeIdentityReader().command_member(
                    baseline.pid, os.getpgrp(), b"KORA_SCOPED_SHELL=fixture-probe"
                )
                assert state is CommandRead.OPAQUE
            descriptors = gate_descriptors()
            peer = start_gated(
                ProcessLaunch(
                    executable=sys.executable,
                    argv=(
                        sys.executable,
                        "-I",
                        "-c",
                        "import os; os.write(1, b'ready\\n'); "
                        "os.write(1, os.read(0, 1))",
                    ),
                    environment=(
                        EnvironmentVariable(
                            name="KORA_ATTEMPT_TOKEN", value=ATTEMPT_MARKER
                        ),
                    ),
                    working_directory_resource_id=new_operation_id(),
                    stdin=None,
                    stdout=None,
                    stderr=ProcessOutput.CAPTURE,
                    domain=ProcessDomain.DETACHED,
                ),
                new_operation_id(),
                checkout.anchor.descriptor,
                before(clock),
                clock,
                stdio_limit=8192,
                wait_owner=reaper,
            )
            try:
                sibling = await peer.prepare()
                registrations.append(sibling)
                await peer.acknowledge()
                reader, writer = peer.borrow_stdio()
                assert await asyncio.wait_for(reader.readline(), 4) == b"ready\n"
                invocation = admit(clock, python_command(DESCENDANT))
                call = asyncio.create_task(owner.run(invocation))
                await until(
                    lambda: (
                        (workspace / "ready").exists()
                        and (workspace / "descendant").exists()
                    )
                )
                member = current_identity(workspace / "descendant")
                leader = current_identity(workspace / "leader")
                observed = NativeIdentityReader().read(member.pid).process
                assert observed is not None
                assert observed.process_group_id != leader.pid
                if sys.platform == "linux":
                    assert observed.parent_pid == os.getpid()
                (workspace / "finish").touch()
                assert await call == ScopedShellResult(0, "finished\n")
                assert not owner.occupied
                assert (workspace / "executions").read_text() == "ran"
                with pytest.raises(ScopedShellError) as replay:
                    await owner.run(invocation)
                assert replay.value.reason is ScopedShellFailure.REJECTED
                assert_gone(leader)
                assert_gone(member)
                assert baseline.poll() is None
                writer.write(b"k")
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(1), 4) == b"k"
                await peer.settle(before(clock))
                await peer.drain(before(clock))
                assert peer.release(before(clock)) == 0
                assert_gone(sibling.process)
            finally:
                if not peer.closed:
                    peer.signal(signal.SIGKILL, before(clock))
                    await peer.settle(before(clock))
                    await peer.drain(before(clock))
                    peer.release(before(clock))
            assert gate_descriptors() == descriptors


class ExpiringClock(SystemClock):
    offset: int = 0

    def monotonic_ns(self) -> int:
        return super().monotonic_ns() + self.offset

    def expire(self, cutoff: int) -> None:
        self.offset = cutoff - super().monotonic_ns()


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("bash", ScopedShellFailure.REJECTED),
        ("checkout", ScopedShellFailure.REJECTED),
        ("stop", ScopedShellFailure.STOPPED),
        ("expiry", ScopedShellFailure.EXPIRED),
        ("denial", ScopedShellFailure.REJECTED),
        ("cancel", None),
    ],
)
@pytest.mark.asyncio
async def test_scoped_shell_refuses_changed_inputs_or_stopped_activation(
    tmp_path: Path,
    shell_diagnostics: Capture,
    condition: str,
    expected: ScopedShellFailure | None,
) -> None:
    workspace = tmp_path.resolve() / "checkout"
    workspace.mkdir(mode=0o700)
    moved = workspace.with_name("moved")
    clock = ExpiringClock()
    total = open_descriptors()
    async with shell_owner(
        workspace, shell_diagnostics, clock, altered_stamp=condition == "bash"
    ) as (
        owner,
        _,
        _,
    ):
        if condition == "checkout":
            workspace.rename(moved)
            workspace.mkdir(mode=0o700)
        authorizations = 0
        descriptors = gate_descriptors()

        def current() -> bool:
            nonlocal authorizations
            authorizations += 1
            if authorizations == 2:
                assert owner.occupied
                assert len(gate_descriptors()) > len(descriptors)
                if condition == "stop":
                    owner.stop()
                if condition == "expiry":
                    clock.expire(invocation.cutoff_ns)
                if condition == "cancel":
                    call.cancel()
                if condition == "denial":
                    return False
            return True

        invocation = admit(clock, "printf invoked > effect", current)
        call = asyncio.create_task(owner.run(invocation))
        if expected is None:
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            with pytest.raises(ScopedShellError) as refused:
                await call
            assert refused.value.reason is expected
        assert authorizations == (1 if condition in ("bash", "checkout") else 2)
        assert not owner.occupied
        assert not (workspace / "effect").exists()
        assert not (moved / "effect").exists()
        assert gate_descriptors() == descriptors
    assert open_descriptors() == total


OUTPUT_FIXTURE: Final[str] = (
    WRITE_PID
    + """
import os
import stat
import sys
import time
descriptor = int(sys.argv[1])
assert os.read(0, 1) == b''
assert stat.S_ISCHR(os.fstat(0).st_mode)
assert stat.S_ISFIFO(os.fstat(1).st_mode)
assert stat.S_ISFIFO(os.fstat(2).st_mode)
assert os.fstat(1).st_ino != os.fstat(2).st_ino
try:
    os.fstat(descriptor)
except OSError:
    pass
else:
    raise AssertionError('ambient descriptor survived')
shell_created = {'SHLVL', '_', '__CF_USER_TEXT_ENCODING', 'LC_CTYPE'}
assert set(os.environ) - shell_created == set(sys.argv[2].split(','))
assert os.environ['PWD'] == os.getcwd()
assert os.environ['PATH'] == '/usr/bin:/bin'
assert os.environ['HOME'] == os.path.join(os.getcwd(), 'home')
assert os.environ['TMPDIR'] == os.path.join(os.getcwd(), 'tmp')
assert len(os.environ['KORA_SCOPED_SHELL']) == 32
assert os.environ['KORA_ATTEMPT_TOKEN'] == sys.argv[3]
record('masked.pid', os.getpid())
os.write(2, b'private-stderr-marker' * 17000)
os.write(1, b'private-sens')
while not Path('first').exists():
    time.sleep(.01)
os.write(1, b'itive-split \\xe2')
while not Path('second').exists():
    time.sleep(.01)
os.write(1, b'\\x82\\xac:\\xe2')
"""
)


@pytest.mark.asyncio
async def test_scoped_shell_bounds_and_masks_stdout_through_eof(
    tmp_path: Path,
    shell_diagnostics: Capture,
) -> None:
    workspace = tmp_path.resolve()
    clock = SystemClock()
    shell_diagnostics.scope.admit(PRIVATE_TEXT)
    async with shell_owner(workspace, shell_diagnostics, clock) as (owner, _, _):
        black = os.open(os.devnull, os.O_RDONLY)
        inherited = fcntl.fcntl(black, fcntl.F_DUPFD, 129)
        try:
            assert os.get_inheritable(inherited)
            descriptors = gate_descriptors()
            environment = ",".join(
                (
                    "HOME",
                    "TMPDIR",
                    "PATH",
                    "PWD",
                    "KORA_ATTEMPT_TOKEN",
                    SCOPE_VARIABLE,
                )
            )
            invocation = admit(
                clock,
                python_command(
                    OUTPUT_FIXTURE, str(inherited), environment, ATTEMPT_MARKER
                ),
            )
            task = asyncio.create_task(owner.run(invocation))
            await until(
                lambda: owner.stdout_bytes == len(b"private-sens") or task.done()
            )
            assert not task.done()
            leader = current_identity(workspace / "masked.pid")
            (workspace / "first").touch()
            await until(
                lambda: owner.stdout_bytes == len(PRIVATE_TEXT) + 2 or task.done()
            )
            assert not task.done()
            (workspace / "second").touch()
            assert await task == ScopedShellResult(0, "[redacted] €:�")
            assert_gone(leader)
            assert gate_descriptors() == descriptors

            raw = "q" * OUTPUT_BYTES
            shell_diagnostics.scope.admit("q" * 32)
            fixture = "import os, sys; os.write(1, sys.argv[1].encode())"
            admitted = admit(clock, python_command(fixture, raw))
            assert await owner.run(admitted) == ScopedShellResult(0, "[redacted]")

            overhead = len(
                json.dumps({"exit_code": 0, "stdout": ""}, separators=(",", ":"))
            )
            exact = "\n" * ((OUTPUT_BYTES - overhead) // 2) + "v" * (
                (OUTPUT_BYTES - overhead) % 2
            )
            allowed = admit(clock, python_command(fixture, exact))
            assert await owner.run(allowed) == ScopedShellResult(0, exact)
            for output in (raw + "q", exact + "v"):
                source = (
                    WRITE_PID
                    + """
import os, signal, sys
record('overflow.pid', os.getpid())
os.write(1, sys.argv[1].encode())
signal.pause()
"""
                )
                with pytest.raises(ScopedShellError) as overflow:
                    await owner.run(admit(clock, python_command(source, output)))
                assert overflow.value.reason is ScopedShellFailure.OVERFLOW
                overflow_pid = int((workspace / "overflow.pid").read_text())
                assert (
                    NativeIdentityReader().read(overflow_pid).state
                    is IdentityState.ABSENT
                )
                assert not owner.occupied
                assert gate_descriptors() == descriptors
        finally:
            os.close(inherited)
            os.close(black)

    records = shell_diagnostics.finish()
    assert any(record["event"] == SCOPED_SHELL.name for record in records)
    text = repr(records)
    for private in (
        PRIVATE_TEXT,
        ATTEMPT_MARKER,
        "private-stderr-marker",
        "private-sens",
        raw,
    ):
        assert private not in text


SEND_STDERR: Final[str] = (
    WRITE_PID
    + """
import array
import os
import signal
import socket
import sys
record('retained.pid', os.getpid())
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as relay:
    relay.connect(sys.argv[1])
    descriptor = array.array('i', [2])
    relay.sendmsg([b'd'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor)])
os.write(1, b'private-withheld-result')
signal.pause()
"""
)


def receive_stderr(listener: socket.socket) -> int:
    accepted, _ = listener.accept()
    with accepted:
        accepted.settimeout(4)
        received = array("i")
        data, ancillary, _flags, _address = accepted.recvmsg(
            1, socket.CMSG_SPACE(received.itemsize)
        )
        assert data == b"d"
        assert len(ancillary) == 1
        level, kind, descriptor = ancillary[0]
        assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
        received.frombytes(descriptor)
        assert len(received) == 1
        return received[0]


@pytest.mark.asyncio
async def test_scoped_shell_keeps_unresolved_ownership(
    tmp_path: Path,
    shell_diagnostics: Capture,
) -> None:
    workspace = tmp_path.resolve()
    clock = SystemClock()
    async with shell_owner(workspace, shell_diagnostics, clock) as (owner, _, _):
        descriptors = gate_descriptors()
        with (
            short_directory() as short,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
        ):
            socket_path = short / "relay"
            listener.bind(str(socket_path))
            listener.listen()
            listener.settimeout(4)
            cmd = python_command(SEND_STDERR, str(socket_path))
            task = asyncio.create_task(owner.run(admit(clock, cmd)))
            retained = await asyncio.to_thread(receive_stderr, listener)
            try:
                leader = current_identity(workspace / "retained.pid")
                owner.stop()
                replacement = admit(clock, "printf replacement")
                with pytest.raises(ScopedShellError) as busy:
                    await owner.run(replacement)
                assert busy.value.reason is ScopedShellFailure.BUSY
                with pytest.raises(ScopedShellError) as retained_owner:
                    await task
                assert retained_owner.value.reason is ScopedShellFailure.UNRESOLVED
                assert owner.occupied
                assert os.write(retained, b"still held") == len(b"still held")
                with pytest.raises(ScopedShellError) as still_busy:
                    await owner.run(replacement)
                assert still_busy.value.reason is ScopedShellFailure.BUSY
            finally:
                os.close(retained)
        assert await owner.settle(before(clock))
        assert not owner.occupied
        assert_gone(leader)
        assert gate_descriptors() == descriptors
        assert await owner.run(replacement) == ScopedShellResult(0, "replacement")
