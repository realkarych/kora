import asyncio
import os
import select
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Protocol, TypeVar

from kora.executor.files import write_all
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND

READ_SIZE: Final[int] = 65_536
DEFAULT_KILL_GRACE_NS: Final[int] = 2 * NANOSECONDS_PER_SECOND
DEFAULT_EOF_BOUND_NS: Final[int] = 5 * NANOSECONDS_PER_SECOND
KIB: Final[int] = 1024
MAX_STREAM_STDERR: Final[int] = 4096


class Ending(StrEnum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    stdin: bytes | None
    timeout_ns: int
    stdout_limit: int
    stderr_limit: int


@dataclass(frozen=True)
class StepOutcome:
    ending: Ending
    exit_code: int | None
    stdout: bytes
    stdout_overflow: bool
    stderr: bytes
    stderr_dropped: int
    peak_rss_bytes: int

    @property
    def succeeded(self) -> bool:
        return self.ending is Ending.EXITED and self.exit_code == 0


class StepLauncher(Protocol):
    async def run(self, spec: LaunchSpec) -> StepOutcome: ...


def seconds(nanoseconds: int) -> float:
    return nanoseconds / NANOSECONDS_PER_SECOND


def rss_bytes(maximum: int) -> int:
    return maximum if sys.platform == "darwin" else maximum * KIB


class PipeDrain:
    def __init__(
        self, loop: asyncio.AbstractEventLoop, descriptor: int, limit: int
    ) -> None:
        self._loop = loop
        self._descriptor = descriptor
        self._limit = limit
        self._kept = bytearray()
        self.received = 0
        self.closed = asyncio.Event()
        os.set_blocking(descriptor, False)
        loop.add_reader(descriptor, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self._descriptor, READ_SIZE)
        except BlockingIOError:
            return
        if not data:
            self._stop()
            return
        self.received += len(data)
        room = self._limit - len(self._kept)
        if room > 0:
            self._kept += data[:room]

    def _stop(self) -> None:
        if not self.closed.is_set():
            self._loop.remove_reader(self._descriptor)
            self.closed.set()

    @property
    def kept(self) -> bytes:
        return bytes(self._kept)

    @property
    def overflow(self) -> bool:
        return self.received > self._limit

    @property
    def dropped(self) -> int:
        return max(0, self.received - self._limit)

    def close(self) -> None:
        self._stop()
        os.close(self._descriptor)


class PipeFeed:
    def __init__(
        self, loop: asyncio.AbstractEventLoop, descriptor: int, data: bytes
    ) -> None:
        self._loop = loop
        self._descriptor = descriptor
        self._data = memoryview(data)
        self._open = True
        if not data:
            self.close()
            return
        os.set_blocking(descriptor, False)
        loop.add_writer(descriptor, self._writable)

    def _writable(self) -> None:
        try:
            written = os.write(self._descriptor, self._data[:READ_SIZE])
        except BlockingIOError:
            return
        except BrokenPipeError:
            self.close()
            return
        self._data = self._data[written:]
        if not self._data:
            self.close()

    def close(self) -> None:
        if self._open:
            self._open = False
            self._loop.remove_writer(self._descriptor)
            os.close(self._descriptor)


class ExitWatch:
    def __init__(self, loop: asyncio.AbstractEventLoop, pid: int) -> None:
        self._loop = loop
        self._pid = pid
        self.exited = asyncio.Event()
        if sys.platform == "linux":
            self._descriptor = os.pidfd_open(pid)
            loop.add_reader(self._descriptor, self._notified)
        else:
            self._queue = select.kqueue()
            self._descriptor = self._queue.fileno()
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                self._queue.control([event], 0)
            except ProcessLookupError:
                self.exited.set()
            else:
                loop.add_reader(self._descriptor, self._notified)

    def _notified(self) -> None:
        self._loop.remove_reader(self._descriptor)
        self.exited.set()

    def signal(self, number: signal.Signals) -> None:
        if sys.platform == "linux":
            signal.pidfd_send_signal(self._descriptor, number)
        else:
            os.kill(self._pid, number)

    def close(self) -> None:
        if not self.exited.is_set():
            self._loop.remove_reader(self._descriptor)
        if sys.platform == "linux":
            os.close(self._descriptor)
        else:
            self._queue.close()


def spawned(
    argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]
) -> tuple["subprocess.Popen[bytes]", int, int, int]:
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    try:
        process = subprocess.Popen(
            argv,
            executable=argv[0],
            cwd=cwd,
            env=dict(env),
            stdin=stdin_read,
            stdout=stdout_write,
            stderr=stderr_write,
            close_fds=True,
            shell=False,
        )
    except OSError:
        for descriptor in (stdin_write, stdout_read, stderr_read):
            os.close(descriptor)
        raise
    finally:
        for descriptor in (stdin_read, stdout_write, stderr_write):
            os.close(descriptor)
    return process, stdin_write, stdout_read, stderr_read


class Closable(Protocol):
    def close(self) -> None: ...


ClosableT = TypeVar("ClosableT", bound=Closable)


class Built:
    def __init__(
        self, process: "subprocess.Popen[bytes]", descriptors: list[int]
    ) -> None:
        self._process = process
        self._descriptors = descriptors
        self._parts: list[Closable] = []

    def adopt(self, part: ClosableT) -> ClosableT:
        self._parts.append(part)
        return part

    def own(self, part: ClosableT, descriptor: int) -> ClosableT:
        self._descriptors.remove(descriptor)
        return self.adopt(part)

    def __enter__(self) -> "Built":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if kind is None:
            return
        for part in reversed(self._parts):
            part.close()
        for descriptor in self._descriptors:
            os.close(descriptor)
        self._process.kill()
        self._process.wait()


@dataclass(frozen=True)
class Reaped:
    exit_code: int
    peak_rss_bytes: int


class ChildProcess:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        stdin: bytes,
        stdout_limit: int,
        stderr_limit: int,
    ) -> None:
        self._popen, stdin_write, stdout_read, stderr_read = spawned(argv, cwd, env)
        self.pid = self._popen.pid
        self.reaped: Reaped | None = None
        built = Built(self._popen, [stdin_write, stdout_read, stderr_read])
        with built:
            self._watch = built.adopt(ExitWatch(loop, self.pid))
            self._feed = built.own(PipeFeed(loop, stdin_write, stdin), stdin_write)
            self.stdout = built.own(
                PipeDrain(loop, stdout_read, stdout_limit), stdout_read
            )
            self.stderr = built.own(
                PipeDrain(loop, stderr_read, stderr_limit), stderr_read
            )

    async def wait_exit(self, timeout_ns: int) -> bool:
        try:
            await asyncio.wait_for(self._watch.exited.wait(), seconds(timeout_ns))
        except TimeoutError:
            return False
        return True

    def send(self, number: signal.Signals) -> None:
        if self.reaped is None and not self._watch.exited.is_set():
            self._watch.signal(number)

    def reap(self) -> Reaped:
        if self.reaped is None:
            _, status, usage = os.wait4(self.pid, 0)
            code = os.waitstatus_to_exitcode(status)
            self._popen.returncode = code
            self.reaped = Reaped(code, rss_bytes(usage.ru_maxrss))
        return self.reaped

    async def wait_pipes(self, bound_ns: int) -> bool:
        waits = asyncio.gather(self.stdout.closed.wait(), self.stderr.closed.wait())
        try:
            await asyncio.wait_for(waits, seconds(bound_ns))
        except TimeoutError:
            return False
        return True

    async def terminate(self, grace_ns: int) -> None:
        self.send(signal.SIGTERM)
        if not await self.wait_exit(grace_ns):
            self.send(signal.SIGKILL)
            await self._watch.exited.wait()

    def kill_now(self) -> None:
        if self.reaped is None:
            self.send(signal.SIGKILL)
            self.reap()

    def close(self) -> None:
        self._feed.close()
        self.stdout.close()
        self.stderr.close()
        self._watch.close()


class ProcessLauncher(StepLauncher):
    def __init__(
        self,
        kill_grace_ns: int = DEFAULT_KILL_GRACE_NS,
        eof_bound_ns: int = DEFAULT_EOF_BOUND_NS,
    ) -> None:
        self.kill_grace_ns = kill_grace_ns
        self.eof_bound_ns = eof_bound_ns

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        child = ChildProcess(
            asyncio.get_running_loop(),
            spec.argv,
            spec.cwd,
            spec.env,
            spec.stdin or b"",
            spec.stdout_limit,
            spec.stderr_limit,
        )
        try:
            return await self._supervise(child, spec.timeout_ns)
        except asyncio.CancelledError:
            try:
                await child.terminate(self.kill_grace_ns)
                child.reap()
                await child.wait_pipes(self.eof_bound_ns)
            finally:
                child.kill_now()
            raise
        finally:
            child.kill_now()
            child.close()

    async def _supervise(self, child: ChildProcess, timeout_ns: int) -> StepOutcome:
        ending = Ending.EXITED
        if not await child.wait_exit(timeout_ns):
            ending = Ending.TIMED_OUT
            await child.terminate(self.kill_grace_ns)
        reaped = child.reap()
        if not await child.wait_pipes(self.eof_bound_ns):
            ending = Ending.UNKNOWN
        return StepOutcome(
            ending=ending,
            exit_code=reaped.exit_code if ending is Ending.EXITED else None,
            stdout=child.stdout.kept,
            stdout_overflow=child.stdout.overflow,
            stderr=child.stderr.kept,
            stderr_dropped=child.stderr.dropped,
            peak_rss_bytes=reaped.peak_rss_bytes,
        )


@dataclass(frozen=True)
class Completed:
    exit_code: int
    stdout: bytes


def run_sync(
    argv: tuple[str, ...], cwd: Path, env: Mapping[str, str], timeout_ns: int
) -> Completed:
    completed = subprocess.run(
        argv,
        executable=argv[0],
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=seconds(timeout_ns),
        check=False,
        shell=False,
    )
    return Completed(completed.returncode, completed.stdout)


class PipeStream:
    def __init__(
        self, loop: asyncio.AbstractEventLoop, descriptor: int, high_water: int
    ) -> None:
        self._loop = loop
        self._descriptor = descriptor
        self._high_water = high_water
        self._buffer = bytearray()
        self._eof = False
        self._reading = True
        self._ready = asyncio.Event()
        os.set_blocking(descriptor, False)
        loop.add_reader(descriptor, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self._descriptor, READ_SIZE)
        except BlockingIOError:
            return
        if not data:
            self._eof = True
            self._pause()
        self._buffer += data
        if len(self._buffer) >= self._high_water:
            self._pause()
        self._ready.set()

    def _pause(self) -> None:
        if self._reading:
            self._reading = False
            self._loop.remove_reader(self._descriptor)

    def _resume(self) -> None:
        if not self._reading and not self._eof:
            self._reading = True
            self._loop.add_reader(self._descriptor, self._readable)

    async def _fill(self) -> None:
        if self._eof:
            raise EOFError
        self._ready.clear()
        self._resume()
        await self._ready.wait()

    async def read_line(self, limit: int) -> bytes:
        while True:
            end = self._buffer.find(b"\n", 0, limit + 1)
            if end >= 0:
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            if len(self._buffer) > limit:
                raise EOFError
            await self._fill()

    async def read_some(self, limit: int) -> bytes:
        while not self._buffer:
            await self._fill()
        data = bytes(self._buffer[:limit])
        del self._buffer[:limit]
        return data

    def close(self) -> None:
        self._pause()
        os.close(self._descriptor)


class StreamingChild:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        high_water: int,
    ) -> None:
        self._popen, self._stdin, stdout_read, stderr_read = spawned(argv, cwd, env)
        self.pid = self._popen.pid
        built = Built(self._popen, [self._stdin, stdout_read, stderr_read])
        with built:
            self._watch = built.adopt(ExitWatch(loop, self.pid))
            self.stdout = built.own(
                PipeStream(loop, stdout_read, high_water), stdout_read
            )
            self._stderr = built.own(
                PipeDrain(loop, stderr_read, MAX_STREAM_STDERR), stderr_read
            )
        self._stdin_open = True
        self._reaped = False

    def write(self, data: bytes) -> None:
        write_all(self._stdin, data)

    def close_stdin(self) -> None:
        if self._stdin_open:
            self._stdin_open = False
            os.close(self._stdin)

    async def stop(self, grace_ns: int) -> int:
        self.close_stdin()
        try:
            await asyncio.wait_for(self._watch.exited.wait(), seconds(grace_ns))
        except TimeoutError:
            if not self._watch.exited.is_set():
                self._watch.signal(signal.SIGKILL)
            await self._watch.exited.wait()
        finally:
            self.kill_now()
        return self._popen.returncode

    def kill_now(self) -> None:
        if not self._reaped:
            if not self._watch.exited.is_set():
                self._watch.signal(signal.SIGKILL)
            _, status, _ = os.wait4(self.pid, 0)
            self._popen.returncode = os.waitstatus_to_exitcode(status)
            self._reaped = True
            self.close_stdin()
            self.stdout.close()
            self._stderr.close()
            self._watch.close()
