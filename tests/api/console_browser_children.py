import asyncio
import ctypes
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from kora.plugins.runtime.local.reaper import adopted_pids

PR_SET_CHILD_SUBREAPER = 36
STDERR_LIMIT = 64 * 1024
RECOVERY_SECONDS = 5


class BrowserStderrOverflow(RuntimeError):
    pass


def adopt_fixture_children() -> None:
    assert sys.platform == "linux"
    control = ctypes.CDLL(None, use_errno=True).prctl
    control.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    control.restype = ctypes.c_int
    if control(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Browser fixture subreaper unavailable.")
    assert adopted_pids() == ()


async def ready_pidfd(descriptor: int) -> None:
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    loop.add_reader(descriptor, ready.set)
    try:
        await ready.wait()
    finally:
        loop.remove_reader(descriptor)


def fixture_pidfd(pid: int) -> int:
    if sys.platform != "linux":
        raise OSError("Linux is required for browser fixture ownership.")
    return os.pidfd_open(pid)


async def wait_stopped(descriptor: int) -> None:
    if sys.platform != "linux":
        raise OSError("Linux is required for browser fixture ownership.")
    async with asyncio.timeout(5):
        while (
            os.waitid(os.P_PIDFD, descriptor, os.WSTOPPED | os.WNOWAIT | os.WNOHANG)
            is None
        ):
            await asyncio.sleep(0)


async def reap_child(pid: int, terminate: bool) -> None:
    if sys.platform != "linux":
        raise OSError("Linux is required for browser fixture ownership.")
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        try:
            status = os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
        except ChildProcessError:
            return
        if status is not None:
            return
        if terminate:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await ready_pidfd(descriptor)
        assert os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG) is not None
    finally:
        os.close(descriptor)


async def release_adopted(
    preserve: frozenset[int] = frozenset(),
    *,
    terminate: bool = True,
) -> None:
    async with asyncio.timeout(RECOVERY_SECONDS):
        while True:
            children = adopted_pids()
            if children is not None:
                owned = set(children) - preserve
                if not owned:
                    return
                for pid in owned:
                    await reap_child(pid, terminate)
            await asyncio.sleep(0)


async def capture_stderr(reader: asyncio.StreamReader) -> bytes:
    captured = bytearray()
    while chunk := await reader.read(8192):
        if len(chunk) > STDERR_LIMIT - len(captured):
            raise BrowserStderrOverflow("Browser diagnostics exceeded their bound.")
        captured.extend(chunk)
    return bytes(captured)


async def discard(reader: asyncio.StreamReader) -> None:
    while await reader.read(8192):
        continue


def direct_pidfd(process: asyncio.subprocess.Process) -> int | None:
    if sys.platform != "linux":
        raise OSError("Linux is required for browser fixture ownership.")
    try:
        descriptor = os.pidfd_open(process.pid)
    except ProcessLookupError:
        return None
    try:
        os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    except ChildProcessError:
        os.close(descriptor)
        return None
    return descriptor


def signal_owned(descriptor: int, requested: signal.Signals) -> None:
    if sys.platform != "linux":
        raise OSError("Linux is required for browser fixture ownership.")
    try:
        signal.pidfd_send_signal(descriptor, requested)
    except ProcessLookupError:
        pass


async def settle_node(
    process: asyncio.subprocess.Process,
    descriptor: int | None,
    preserve: frozenset[int],
    *,
    stop: bool,
    grace: float,
) -> None:
    if descriptor is not None:
        try:
            if stop:
                signal_owned(descriptor, signal.SIGTERM)
            try:
                async with asyncio.timeout(grace):
                    await ready_pidfd(descriptor)
            except TimeoutError:
                signal_owned(descriptor, signal.SIGKILL)
                async with asyncio.timeout(RECOVERY_SECONDS):
                    await ready_pidfd(descriptor)
        finally:
            os.close(descriptor)
    await release_adopted(preserve | {process.pid}, terminate=stop)
    assert process.stdout is not None
    assert process.stderr is not None
    async with asyncio.timeout(RECOVERY_SECONDS):
        await asyncio.gather(
            process.wait(), discard(process.stdout), discard(process.stderr)
        )


async def run_node(
    node: str,
    entry: Path,
    arguments: tuple[str, ...],
    root: Path,
    environment: dict[str, str],
    exercise: Callable[[asyncio.subprocess.Process, int | None], Awaitable[None]],
    *,
    preserve: frozenset[int] = frozenset(),
    grace: float = 15,
) -> tuple[int, bytes]:
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            node,
            str(entry),
            *arguments,
            cwd=root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    complete = False
    descriptor: int | None = None
    exchange: asyncio.Task[None] | None = None
    stderr: asyncio.Task[bytes] | None = None

    async def finish() -> None:
        process = await asyncio.shield(launch)
        for task in (exchange, stderr):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await settle_node(
            process,
            descriptor if descriptor is not None else direct_pidfd(process),
            preserve,
            stop=not complete,
            grace=grace,
        )

    async def run_exchange(process: asyncio.subprocess.Process) -> None:
        await exercise(process, descriptor)

    try:
        process = await asyncio.shield(launch)
        descriptor = direct_pidfd(process)
        assert process.stderr is not None
        exchange = asyncio.create_task(run_exchange(process))
        stderr = asyncio.create_task(capture_stderr(process.stderr))
        async with asyncio.timeout(125):
            await asyncio.gather(exchange, stderr)
        complete = True
    finally:
        settlement = asyncio.create_task(finish())
        interrupted = False
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                interrupted = True
        settlement.result()
        if interrupted:
            raise asyncio.CancelledError
    assert process.returncode is not None
    return process.returncode, stderr.result()
