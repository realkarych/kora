import asyncio
import os
import signal
import stat
import sys
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final

from kora.executor.launcher import ExitWatch
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
    def __init__(self, pid: int, watch: ExitWatch | None) -> None:
        self.pid = pid
        self.watch = watch
        self.reaped = False
        self.closed = False

    def reap(self) -> bool:
        if self.reaped:
            return True
        options = (
            0 if self.watch is not None and self.watch.exited.is_set() else (os.WNOHANG)
        )
        try:
            finished, _ = os.waitpid(self.pid, options)
        except ChildProcessError:
            finished = self.pid
        self.reaped = finished == self.pid
        return self.reaped

    async def settle(self, clock: Clock, bound_ns: int, *, kill: bool) -> bool:
        if self.watch is None:
            self.watch = exit_watch(self.pid)
        if self.watch is not None and self.watch.exited.is_set():
            return self.reap()
        if kill:
            if self.watch is not None:
                self.watch.signal(signal.SIGKILL)
            elif self.reap():
                return True
            else:
                os.kill(self.pid, signal.SIGKILL)
        if self.watch is not None:
            await until(self.watch.exited, clock, clock.monotonic_ns() + bound_ns)
        return self.reap()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.watch is not None:
                self.watch.close()


def launcher_of(pid: int) -> Launcher:
    return Launcher(pid, exit_watch(pid))


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
            launcher.close()
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


async def read_readiness(read_end: int, clock: Clock, deadline_ns: int) -> Readiness:
    waiter = DescriptorReadiness(read_end, writing=False)
    try:
        await wait_future(waiter.ready, deadline_ns, clock)
    except ChannelError as error:
        if error.reason is not ChannelFailure.DEADLINE:
            raise
        return Readiness.TIMEOUT
    finally:
        waiter.close()
    return Readiness.READY if os.read(read_end, 1) == READY else Readiness.ENDED


async def spawn_runner(
    argv: Sequence[str],
    environment: Mapping[str, str],
    clock: Clock,
    launchers: Launchers,
) -> Readiness:
    if launchers.closed:
        raise refusal("runtime_closed")
    spawning = asyncio.get_running_loop().create_future()
    launchers.spawning.add(spawning)
    try:
        await launchers.retry(clock)
        read_end, write_end = readiness_pipe()
        try:
            try:
                pid = start_launcher(argv, environment, write_end)
            finally:
                os.close(write_end)
            outcome = Readiness.TIMEOUT
            try:
                deadline = clock.monotonic_ns() + READINESS_DEADLINE_NS
                outcome = await read_readiness(read_end, clock, deadline)
                return outcome
            finally:
                kill = outcome is Readiness.TIMEOUT
                await launchers.settled(launcher_of(pid), clock, kill=kill)
        finally:
            os.close(read_end)
    finally:
        launchers.spawning.discard(spawning)
        spawning.set_result(None)
