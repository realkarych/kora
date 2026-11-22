import asyncio
import os
import signal
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Final

from kora.executor.launcher import ExitWatch
from kora.plugins.runtime.local.child_owner import ChildOwner
from kora.protocols.deadlines import ExecutionDeadline, thresholds
from kora.protocols.identity import ProcessIdentity
from kora.protocols.time import Clock
from kora.runner.channels import (
    ChannelError,
    ChannelFailure,
    DescriptorReadiness,
    cancel_task,
    wait_future,
)
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_created_directory,
)
from kora.runner.registry import (
    READINESS_DESCRIPTOR,
    READY,
    SOCKET_NAME_LENGTH,
    SOCKET_PATH_LENGTH,
    AttemptKey,
    FirstGrantHead,
    model_bytes,
    refusal,
    socket_path,
)

READINESS_DEADLINE_NS: Final[int] = 30_000_000_000
LAUNCHER_EXIT_NS: Final[int] = 5_000_000_000
SYSTEM_PATH: Final[str] = "/usr/bin:/bin"
DARWIN_USER_TEMP_DIR: Final[int] = 65537
RUNNER_MODULE: Final[str] = "kora.plugins.runtime.local.supervisor"
LAUNCHER: Final[str] = "\n".join(
    [
        "import os, sys",
        "os.setsid()",
        "os.closerange(4, os.sysconf('SC_OPEN_MAX'))",
        "if os.fork() == 0:",
        "    os.execv(sys.argv[1], sys.argv[1:])",
        "os._exit(0)",
    ]
)
DEFAULT_SIGNALS: Final[frozenset[signal.Signals]] = frozenset(
    {
        signal.SIGPIPE,
        signal.SIGXFSZ,
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGCHLD,
        signal.SIGHUP,
        signal.SIGQUIT,
    }
)


class Readiness(StrEnum):
    READY = "ready"
    ENDED = "ended"
    TIMEOUT = "timeout"


def darwin_temporary_directory() -> Path:
    path = Path(os.confstr(DARWIN_USER_TEMP_DIR) or "")
    if not path.is_absolute() or len(path.parts) < 2:
        raise FilesystemError("temporary_directory_unavailable")
    if path.parts[1] != "var":
        return path
    var = os.lstat("/var")
    if (
        not stat.S_ISLNK(var.st_mode)
        or var.st_uid != 0
        or os.readlink("/var") != "private/var"
    ):
        raise FilesystemError("temporary_directory_unsafe")
    return Path("/private", *path.parts[1:])


def default_socket_directory(platform: str, uid: int) -> Path:
    if platform == "darwin":
        return darwin_temporary_directory() / "kora"
    return Path("/", "tmp", f"kora-{uid}")


def open_socket_directory(path: Path) -> DirectoryAnchor:
    if len(os.fsencode(socket_path(str(path), "0" * SOCKET_NAME_LENGTH))) > (
        SOCKET_PATH_LENGTH
    ):
        raise FilesystemError("socket_path_too_long")
    return open_created_directory(path)


def ensure_dispatch_group_leader() -> None:
    if os.getpgid(0) != os.getpid():
        os.setpgid(0, 0)


def runner_command() -> tuple[str, ...]:
    return (sys.executable, "-I", "-m", RUNNER_MODULE)


def runner_environment(platform: str, home: Path) -> dict[str, str]:
    environment = {"PATH": SYSTEM_PATH, "HOME": str(home), "LANG": "C.UTF-8"}
    if platform == "darwin":
        environment["LANG"] = "en_US.UTF-8"
        environment["TMPDIR"] = str(darwin_temporary_directory())
    return environment


def above_readiness(descriptor: int) -> int:
    if descriptor != READINESS_DESCRIPTOR:
        return descriptor
    duplicate = os.dup(descriptor)
    os.close(descriptor)
    return duplicate


def readiness_pipe() -> tuple[int, int]:
    read_end, write_end = os.pipe()
    try:
        os.set_blocking(read_end, False)
        write_end = above_readiness(write_end)
    except BaseException:
        os.close(read_end)
        os.close(write_end)
        raise
    return read_end, write_end


def start_launcher(
    argv: Sequence[str], environment: Mapping[str, str], write_end: int
) -> int:
    null = os.devnull
    return os.posix_spawn(
        sys.executable,
        [sys.executable, "-I", "-c", LAUNCHER, *argv],
        environment,
        file_actions=[
            (os.POSIX_SPAWN_OPEN, 0, null, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, null, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_OPEN, 2, null, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_DUP2, write_end, READINESS_DESCRIPTOR),
        ],
        setsigmask=(),
        setsigdef=DEFAULT_SIGNALS,
    )


async def until(event: asyncio.Event, clock: Clock, deadline_ns: int) -> bool:
    if event.is_set():
        return True
    waiting = asyncio.create_task(event.wait())
    sleeping = asyncio.create_task(clock.sleep_until(deadline_ns))
    try:
        await asyncio.wait({waiting, sleeping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await cancel_task(waiting)
        await cancel_task(sleeping)
    return event.is_set()


def exit_watch(pid: int) -> ExitWatch | None:
    try:
        return ExitWatch(asyncio.get_running_loop(), pid)
    except OSError:
        return None


class Launcher:
    def __init__(self) -> None:
        self.watch: ExitWatch | None = None
        self.closed = False
        self.child = ChildOwner()

    @property
    def reaped(self) -> bool:
        return self.child.collected()

    async def settle(self, clock: Clock, bound_ns: int, *, kill: bool) -> bool:
        cutoff = clock.monotonic_ns() + bound_ns
        with self.child.lock:
            if (
                self.watch is None
                and self.child.pid is not None
                and not self.child.reaped
            ):
                self.watch = exit_watch(self.child.pid)
        if self.reaped:
            return True
        if kill:
            self.child.signal(signal.SIGKILL)
        await until(self.child.exited, clock, cutoff)
        return self.reaped

    async def close(self) -> None:
        if not self.closed:
            self.child.abort()
            watch, self.watch = self.watch, None
            try:
                if watch is not None:
                    watch.close()
            finally:
                try:
                    await self.child.join()
                finally:
                    self.closed = True


def completion_frame(nonce: str, head: FirstGrantHead) -> bytes:
    return b"C" + sha256(nonce.encode("ascii") + model_bytes(head)).digest()


@dataclass
class CompletionPipe:
    key: AttemptKey
    nonce: str
    runner: ProcessIdentity | None = None
    reader: int | None = None
    frame: bytes = b""

    def close(self) -> None:
        reader, self.reader = self.reader, None
        if reader is not None:
            os.close(reader)

    def collect(self) -> bytes:
        if self.reader is not None:
            try:
                frame = os.read(self.reader, 34)
                if len(frame) == 33 and os.read(self.reader, 1) == b"":
                    self.frame = frame
            except BlockingIOError:
                pass
            finally:
                self.close()
        return self.frame


class Launchers:
    def __init__(self, bound_ns: int = LAUNCHER_EXIT_NS) -> None:
        self.bound_ns = bound_ns
        self.unsettled: set[Launcher] = set()
        self.settling: set[asyncio.Task[None]] = set()
        self.spawning: set[asyncio.Future[None]] = set()
        self.closed = False

    async def settle(self, launcher: Launcher, clock: Clock, *, kill: bool) -> None:
        self.unsettled.add(launcher)
        if await launcher.settle(clock, self.bound_ns, kill=kill):
            await launcher.close()
            self.unsettled.discard(launcher)

    async def settled(self, launcher: Launcher, clock: Clock, *, kill: bool) -> None:
        task = asyncio.create_task(self.settle(launcher, clock, kill=kill))
        self.settling.add(task)
        task.add_done_callback(self.settling.discard)
        await asyncio.shield(task)

    async def retry(self, clock: Clock) -> None:
        for launcher in tuple(self.unsettled):
            await self.settle(launcher, clock, kill=True)

    async def drain(self, clock: Clock) -> None:
        self.closed = True
        for pending in (self.spawning, self.settling):
            if pending:
                await asyncio.wait(set(pending))
        await self.retry(clock)


def launch_deadline(clock: Clock, inherited: ExecutionDeadline | None = None) -> int:
    sample = clock.sample()
    cutoff = sample.monotonic_ns + READINESS_DEADLINE_NS
    if inherited is not None:
        cutoff = min(cutoff, thresholds(inherited, sample).interrupt_at_ns)
    return cutoff


async def read_readiness(read_end: int, clock: Clock, deadline_ns: int) -> Readiness:
    if clock.monotonic_ns() >= deadline_ns:
        return Readiness.TIMEOUT
    waiter = DescriptorReadiness(read_end, writing=False)
    try:
        await wait_future(waiter.ready, deadline_ns, clock)
    except ChannelError as error:
        if error.reason is not ChannelFailure.DEADLINE:
            raise
        return Readiness.TIMEOUT
    finally:
        waiter.close()
    if clock.monotonic_ns() >= deadline_ns:
        return Readiness.TIMEOUT
    return Readiness.READY if os.read(read_end, 1) == READY else Readiness.ENDED


async def spawn_runner(
    argv: Sequence[str],
    environment: Mapping[str, str],
    clock: Clock,
    launchers: Launchers,
    *,
    deadline_ns: int | None = None,
    completion: CompletionPipe | None = None,
) -> Readiness:
    if launchers.closed:
        raise refusal("runtime_closed")
    cutoff = launch_deadline(clock)
    if deadline_ns is not None:
        cutoff = min(cutoff, deadline_ns)
    spawning = asyncio.get_running_loop().create_future()
    launchers.spawning.add(spawning)
    try:
        await launchers.retry(clock)
        if clock.monotonic_ns() >= cutoff:
            return Readiness.TIMEOUT
        read_end, write_end = readiness_pipe()
        launcher: Launcher | None = None
        outcome = Readiness.TIMEOUT
        try:
            try:
                if clock.monotonic_ns() >= cutoff:
                    return Readiness.TIMEOUT
                launcher = Launcher()
                if clock.monotonic_ns() >= cutoff:
                    launcher.child.abort()
                    return Readiness.TIMEOUT
                launcher.child.start(
                    partial(start_launcher, argv, environment, write_end)
                )
            finally:
                os.close(write_end)
            outcome = await read_readiness(read_end, clock, cutoff)
        finally:
            try:
                if launcher is not None:
                    if launcher.child.pid is None:
                        await launcher.close()
                    else:
                        await launchers.settled(
                            launcher, clock, kill=outcome is Readiness.TIMEOUT
                        )
            finally:
                if (
                    completion is not None
                    and outcome is Readiness.READY
                    and launcher is not None
                    and launcher.closed
                ):
                    completion.reader = read_end
                else:
                    os.close(read_end)
        return outcome
    finally:
        launchers.spawning.discard(spawning)
        spawning.set_result(None)
