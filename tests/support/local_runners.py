import asyncio
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeVar

import pytest

from kora.executor.digests import object_digest
from kora.ir.json import JsonObject
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.plugins.runtime.local.spawn import runner_command, runner_environment
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import ProcessIdentity
from kora.protocols.runtime import Workload
from kora.runner.identity import IdentityReader, IdentityState, NativeIdentityReader
from kora.runner.registry import (
    READINESS_DESCRIPTOR,
    Entry,
    Fence,
    Starting,
    Submitter,
    Transition,
)
from kora.runner.registry import runner_argv as registry_argv
from tests.plugins.runtime.local.frames_support import StepClock
from tests.protocols.runtime_documents import workload
from tests.support.local_runtime import Identifiers

SHORT_BASE: Final[Path] = Path("/", "tmp")
PS: Final[str] = "/bin/ps"
LISTING_TIMEOUT_SECONDS: Final[float] = 10.0
T = TypeVar("T")


@contextmanager
def short_directory() -> Generator[Path]:
    base = Path(tempfile.mkdtemp(prefix="k", dir=SHORT_BASE)).resolve()
    try:
        yield base
    finally:
        shutil.rmtree(base)


async def open_spawning(
    root: Path,
    ctx: ProtocolContext,
    sockets: Path,
    *,
    runner: Sequence[str] | None = None,
    identities: IdentityReader | None = None,
    new_identifier: Callable[[], str] | None = None,
    home: Callable[[], Path] = Path.home,
    sweep_scan_entries: int = 65536,
) -> LocalRuntime:
    return await LocalRuntime.open(
        LocalRuntimeConfiguration(state_root=str(root)),
        ctx,
        identities=NativeIdentityReader() if identities is None else identities,
        new_identifier=Identifiers() if new_identifier is None else new_identifier,
        runner=runner,
        socket_directory=sockets,
        home=home,
        sweep_scan_entries=sweep_scan_entries,
    )


def submitted(runtime: LocalRuntime, attempt_id: str, **changes: object) -> Workload:
    reader = NativeIdentityReader()
    observation = reader.read(os.getpid())
    assert observation.process is not None
    request: JsonObject = {"node": attempt_id}
    document = workload(
        attempt_id=attempt_id,
        execution_domain_id=runtime.execution_domain_id,
        execution_request=request,
        execution_request_digest=object_digest(request),
        dispatcher_process=observation.process.identity.model_dump(mode="json"),
    )
    return Workload.model_validate_json(json.dumps({**document, **changes}))


def runner_pids(marker: Path) -> tuple[int, ...]:
    listing = subprocess.run(
        [PS, "-A", "-ww", "-o", "pid=", "-o", "args="],
        capture_output=True,
        text=True,
        check=True,
        timeout=LISTING_TIMEOUT_SECONDS,
    )
    found: list[int] = []
    for line in listing.stdout.splitlines():
        pid, _, arguments = line.strip().partition(" ")
        if str(marker) in arguments:
            found.append(int(pid))
    return tuple(pid for pid in found if pid != os.getpid())


def kill_runners(marker: Path) -> tuple[int, ...]:
    reader = NativeIdentityReader()
    killed: list[int] = []
    for pid in runner_pids(marker):
        observation = reader.read(pid)
        if observation.state is IdentityState.PRESENT:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
    return tuple(killed)


def exited_within(pid: int, seconds: float) -> bool:
    try:
        if sys.platform == "linux":
            descriptor = os.pidfd_open(pid)
            try:
                readable, _, _ = select.select([descriptor], [], [], seconds)
                return bool(readable)
            finally:
                os.close(descriptor)
        queue = select.kqueue()
        try:
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            return bool(queue.control([event], 1, seconds))
        finally:
            queue.close()
    except ProcessLookupError:
        return True


def reap_runners(marker: Path) -> tuple[int, ...]:
    killed = kill_runners(marker)
    assert all(exited_within(pid, LISTING_TIMEOUT_SECONDS) for pid in killed)
    return killed


NEVER_READY: Final[tuple[str, ...]] = (
    sys.executable,
    "-I",
    "-c",
    "import signal\nwhile True:\n    signal.pause()",
)
EXITING: Final[tuple[str, ...]] = (sys.executable, "-I", "-c", "pass")
LINGERING: Final[tuple[str, ...]] = (
    sys.executable,
    "-I",
    "-c",
    "\n".join(
        [
            "import os, signal, sys",
            "if sys.platform == 'linux':",
            "    import ctypes",
            "    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)",
            "os.closerange(3, 4)",
            "while True:",
            "    signal.pause()",
        ]
    ),
)


@contextmanager
def carriers(*vectors: Sequence[str]) -> Generator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    try:
        for vector in vectors:
            started.append(subprocess.Popen(list(vector)))
        yield started
    finally:
        for process in started:
            process.kill()
            process.wait(timeout=LISTING_TIMEOUT_SECONDS)


async def lingering_start(
    root: Path,
    sockets: Path,
    ctx: ProtocolContext,
    identities: IdentityReader | None = None,
    sweep_scan_entries: int = 65536,
) -> tuple[LocalRuntime, Starting, tuple[str, ...], int]:
    runtime = await open_spawning(
        root,
        ctx,
        sockets,
        runner=LINGERING,
        identities=identities,
        sweep_scan_entries=sweep_scan_entries,
    )
    with pytest.raises(KoraProtocolError, match="runner_starting"):
        await runtime.submit(submitted(runtime, "attempt-1"), ctx)
    key = runtime.registry.key("attempt-1")
    entry = await runtime.entry(key)
    assert isinstance(entry, Starting)
    argv = registry_argv(entry.runner_prefix, key, entry.nonce)
    spawned = runner_pids(root)
    assert len(spawned) == 1
    return runtime, entry, argv, spawned[0]


def zombie_children() -> tuple[int, ...]:
    listing = subprocess.run(
        [PS, "-A", "-o", "pid=", "-o", "ppid=", "-o", "stat="],
        capture_output=True,
        text=True,
        check=True,
        timeout=LISTING_TIMEOUT_SECONDS,
    )
    found: list[int] = []
    for line in listing.stdout.splitlines():
        pid, parent, state = line.split()[:3]
        if int(parent) == os.getpid() and state.startswith("Z"):
            found.append(int(pid))
    return tuple(found)


def own_identity() -> ProcessIdentity:
    observation = NativeIdentityReader().read(os.getpid())
    assert observation.process is not None
    return observation.process.identity


def starting_entry(
    sockets: Path,
    nonce: str,
    fence: Fence | None = None,
    dispatcher: str = "d-1",
    prefix: tuple[str, ...] = (sys.executable, "/"),
) -> Starting:
    return Starting(
        state="starting",
        deadline=None,
        gate_closed=False,
        request_digest="sha256:" + "1" * 64,
        submitter=Submitter(dispatcher_id=dispatcher, process=own_identity()),
        nonce=nonce,
        started_at=datetime(2026, 10, 29, tzinfo=UTC),
        socket_directory=str(sockets),
        runner_prefix=prefix,
        fence=fence,
    )


async def written(
    runtime: LocalRuntime, attempt_id: str, entry: Entry, ctx: ProtocolContext
) -> None:
    key = runtime.registry.key(attempt_id)
    await runtime.transition(key, lambda _: Transition(entry), ctx, "test")


@dataclass(frozen=True)
class SupervisorExit:
    code: int
    readiness: bytes


async def supervised(argv: Sequence[str], errors: Path | None = None) -> SupervisorExit:
    read_end, write_end = os.pipe()
    actions: list[tuple[int, int, int] | tuple[int, int, str, int, int]] = [
        (os.POSIX_SPAWN_DUP2, write_end, READINESS_DESCRIPTOR)
    ]
    if errors is not None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        actions.append((os.POSIX_SPAWN_OPEN, 2, str(errors), flags, 0o600))
    try:
        pid = os.posix_spawn(
            argv[0],
            list(argv),
            runner_environment(sys.platform, Path.home()),
            file_actions=actions,
        )
    finally:
        os.close(write_end)
    try:
        _, status = await asyncio.to_thread(os.waitpid, pid, 0)
        return SupervisorExit(os.waitstatus_to_exitcode(status), os.read(read_end, 1))
    finally:
        os.close(read_end)


async def driven(clock: StepClock, operation: Awaitable[T], step_ns: int) -> T:
    task = asyncio.ensure_future(operation)
    while not task.done():
        waiting = asyncio.ensure_future(clock.waiting.wait())
        try:
            await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        if not task.done():
            clock.advance(step_ns)
    return task.result()


def runner_argv(
    runtime: LocalRuntime, root: Path, attempt: str, nonce: str
) -> list[str]:
    return [*runner_command(), str(root), attempt, runtime.execution_domain_id, nonce]


async def clock_awaited(clock: StepClock, operation: asyncio.Future[T]) -> None:
    waiting = asyncio.ensure_future(clock.waiting.wait())
    try:
        await asyncio.wait({operation, waiting}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    if operation.done():
        operation.result()
        pytest.fail("the operation ended before it waited on the clock", pytrace=False)
