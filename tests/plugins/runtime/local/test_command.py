import asyncio
import ctypes
import errno
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import LoggingSettings, configure_process
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    FRAME_PROGRESS_NS,
    AttachRequest,
    FrameType,
    control_request,
    read_delivery,
)
from kora.plugins.runtime.local.command import CommandSession
from kora.plugins.runtime.local.command_stop import CommandDomainStop
from kora.plugins.runtime.local.control import (
    attested,
    connected,
    runner_status,
    start_command,
)
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import COMPONENT
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Handle
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import RawEvent, TransferItem, Workload
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import (
    SCAN_NS,
    CommandPIDInventory,
    CommandRead,
    IdentityState,
    NativeIdentityReader,
    darwin_environment,
)
from kora.runner.journal import RunningJournal, read_records
from kora.runner.peers import kernel_source
from kora.runner.registry import Running, Submitter, TransitionExecutor
from tests.support.local_peers import identity_of, until
from tests.support.local_runners import (
    exited_within,
    open_spawning,
    reap_runners,
    runner_pids,
    short_directory,
    submitted,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    open_descriptors,
    prepared,
    private_directory,
    protocol_error,
    recorded,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]

HOLD_MARKER = "kora-hold-809"
FD_REPORT = "\n".join(
    (
        "import os, time",
        "found = []",
        "for descriptor in range(256):",
        "    try:",
        "        os.fstat(descriptor)",
        "    except OSError:",
        "        continue",
        "    found.append(str(descriptor))",
        "with open('fds', 'w') as report:",
        "    report.write(' '.join(found))",
        "time.sleep(30)",
    )
)
PS = "/bin/ps"
HOLD = "\n".join(
    (
        f"marker={HOLD_MARKER!r}",
        "import os, signal, time",
        "reading, writing = os.pipe()",
        "if os.fork() == 0:",
        "    os.close(reading)",
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        "    os.write(writing, b'r')",
        "    os.close(writing)",
        "    time.sleep(60)",
        "    os._exit(0)",
        "os.close(writing)",
        "assert os.read(reading, 1) == b'r'",
        "os._exit(0)",
    )
)
STOPPED_DESCENDANTS = "\n".join(
    (
        f"marker={HOLD_MARKER!r}",
        "import os, signal, sys",
        "from pathlib import Path",
        "def handoff(_number, _frame):",
        "    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})",
        "    if os.fork():",
        "        os._exit(0)",
        "    os.setsid()",
        "    pending = Path('late.pending')",
        "    pending.write_text(str(os.getpid()))",
        "    pending.replace('late')",
        "    signal.signal(signal.SIGTERM, signal.SIG_DFL)",
        "    signal.pthread_sigmask(signal.SIG_SETMASK, mask)",
        "    signal.pause()",
        "    os._exit(1)",
        "children = []",
        "for name in ('grouped', 'detached'):",
        "    reading, writing = os.pipe()",
        "    pid = os.fork()",
        "    if pid == 0:",
        "        os.close(reading)",
        "        if name == 'grouped' and sys.argv[2] == 'unmarked':",
        "            os.set_inheritable(writing, True)",
        '            gate = "import os,signal,sys; '
        "os.write(int(sys.argv[1]), b'r'); signal.pause()\"",
        "            args = [sys.executable, '-I', '-c', gate, str(writing), marker]",
        "            os.execve(sys.executable, args, {})",
        "        if name == 'detached':",
        "            os.setsid()",
        "            if sys.argv[2] == 'resistant':",
        "                signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        "            if sys.argv[2] == 'handoff':",
        "                signal.signal(signal.SIGTERM, handoff)",
        "        os.write(writing, b'r')",
        "        os.close(writing)",
        "        signal.pause()",
        "        os._exit(1)",
        "    os.close(writing)",
        "    assert os.read(reading, 1) == b'r'",
        "    os.close(reading)",
        "    children.append(pid)",
        "pending = Path('children.pending')",
        "pending.write_text(' '.join(map(str, children)))",
        "pending.replace('children')",
        "if sys.argv[2] == 'hold': signal.pause()",
        "sys.exit(int(sys.argv[1]))",
    )
)


def binary(name: str) -> str:
    for candidate in (f"/bin/{name}", f"/usr/bin/{name}"):
        if os.path.exists(candidate):
            return candidate
    pytest.fail(f"{name} is not installed", pytrace=False)


def command_workload(
    runtime: LocalRuntime, attempt: str, cmd: tuple[str, ...], **changes: object
) -> Workload:
    fields: dict[str, object] = {
        "kind": "command",
        "plan_executor": "runner",
        "publish": None,
        "session_spec": None,
        "env": {},
        "secret_refs": {},
        "harness_credentials": {},
        "artifact_inputs": {},
        "resources": [],
        "limits": {},
        "deadline": None,
        "resolved_config": {"cmd": list(cmd), "workspace": {"repo": "local"}},
    }
    fields.update(changes)
    return submitted(runtime, attempt, **fields)


def child_pids(parent: int) -> tuple[int, ...]:
    listing = subprocess.run(
        [PS, "-A", "-o", "pid=", "-o", "ppid="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    found: list[int] = []
    for line in listing.stdout.splitlines():
        pid_text, ppid_text = line.split()
        if int(ppid_text) == parent:
            found.append(int(pid_text))
    return tuple(found)


def marker_pids(marker: str) -> tuple[int, ...]:
    listing = subprocess.run(
        [PS, "-A", "-ww", "-o", "pid=", "-o", "args="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    found: list[int] = []
    for line in listing.stdout.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if marker in args and int(pid_text) != os.getpid():
            found.append(int(pid_text))
    return tuple(found)


def terminate(pid: int) -> None:
    try:
        leader = os.getsid(pid) == pid
    except ProcessLookupError:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not leader:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def release_commands(root: Path) -> None:
    doomed = [
        pid
        for runner in runner_pids(root)
        for pid in (*child_pids(runner), *marker_pids(HOLD_MARKER))
    ]
    for pid in doomed:
        terminate(pid)
    assert all(exited_within(pid, 10) for pid in doomed)


def descriptor_numbers(pid: int) -> tuple[int, ...]:
    if sys.platform == "linux":
        return tuple(sorted(int(name) for name in os.listdir(f"/proc/{pid}/fd")))
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    size = library.proc_pidinfo(pid, 1, 0, None, 0)
    if size <= 0:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure))
    buffer = ctypes.create_string_buffer(size)
    read = library.proc_pidinfo(pid, 1, 0, buffer, size)
    if read <= 0:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure))
    numbers = [
        int.from_bytes(buffer.raw[offset : offset + 4], "little", signed=True)
        for offset in range(0, read - read % 8, 8)
    ]
    return tuple(sorted(numbers))


def descriptor_count(pid: int) -> int:
    return len(descriptor_numbers(pid))


async def running_pid(runtime: LocalRuntime, attempt: str) -> int:
    entry = await recorded(runtime, attempt)
    assert isinstance(entry, Running)
    return entry.runner.pid


@asynccontextmanager
async def launched(
    tmp_path: Path, capture: Capture
) -> AsyncGenerator[tuple[LocalRuntime, ProtocolContext, Path]]:
    root = state_root(tmp_path)
    ctx = runtime_context(capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            yield runtime, ctx, root
        finally:
            await runtime.close()
            release_commands(root)
            reap_runners(root)


def attempt_directory(root: Path, runtime: LocalRuntime, attempt: str) -> Path:
    return root / "attempts" / runtime.registry.key(attempt).name


def fill(directory: Path) -> None:
    descriptor = os.open(
        directory / "filler", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        fill_descriptor(descriptor)
    finally:
        os.close(descriptor)


def fill_descriptor(descriptor: int) -> None:
    for size in (65536, 4096, 1):
        block = b"\0" * size
        while True:
            try:
                os.write(descriptor, block)
            except OSError as error:
                if error.errno != errno.ENOSPC:
                    raise
                break


def journal_events(path: Path) -> tuple[RawEvent, ...]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ()
    complete = raw[: raw.rfind(b"\n") + 1]
    return tuple(RawEvent.model_validate_json(line) for line in complete.splitlines())


async def worker() -> None:
    root = Path(os.environ["KORA_COMMAND_ROOT"])
    sockets = Path(os.environ["KORA_COMMAND_SOCKETS"])
    clock = SystemClock()
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(),
        "command-worker",
        clock.now,
        os.open(os.devnull, os.O_WRONLY),
    )
    ctx = ProtocolContext(
        namespace_id="local-runtime",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger(COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest=None,
        deadline=None,
        clock=clock,
        activity=NoActivity(),
    )
    runtime = await open_spawning(root, ctx, sockets)
    workload = command_workload(runtime, "attempt-1", (binary("sleep"), "60"))
    handle = await runtime.submit(workload, ctx)
    await runtime.start(handle, ctx)
    entry = await recorded(runtime, "attempt-1")
    assert isinstance(entry, Running)
    sys.stdout.write(f"{entry.socket_path}\n")
    sys.stdout.flush()
    signal.pause()


def test_command_journal_ignores_torn_tail(tmp_path: Path) -> None:
    directory = tmp_path / "attempt"
    directory.mkdir(mode=0o700)
    anchor = open_local_directory(directory)

    journal = RunningJournal(anchor, SystemClock())
    try:
        assert journal.append("runner.candidate_started", {"leader": True})
        complete = read_records(anchor)
        with (directory / "journal.jsonl").open("ab") as handle:
            handle.write(b'{"i":1')
        torn = read_records(anchor)
    finally:
        journal.close()
        anchor.close()

    assert complete == ((0, "runner.candidate_started"),)
    assert torn == complete


def descriptors_for(path: Path) -> set[int]:
    expected = path.stat()
    result: set[int] = set()
    for descriptor in descriptor_numbers(os.getpid()):
        try:
            actual = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                result.add(descriptor)
    return result


def live_journal_writers() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name == "kora-journal"}


@pytest.mark.asyncio
async def test_running_journal_releases_owned_descriptors(tmp_path: Path) -> None:
    directory = private_directory(tmp_path, "attempt")
    anchor = open_local_directory(directory)
    path = directory / "journal.jsonl"
    original = descriptors_for(directory)
    writers = live_journal_writers()
    journal = RunningJournal(anchor, SystemClock())

    try:
        written = await asyncio.to_thread(
            journal.append, "runner.candidate_started", {}
        )
        file_descriptors = descriptors_for(path)
        directory_descriptors = descriptors_for(directory)
        records = read_records(anchor)
        anchor.close()
        later = await asyncio.to_thread(journal.append, "transfer.materialised", {})
        journal.close()
        journal.close()
        refused = not journal.append("transfer.rejected", {})
        await until(lambda: live_journal_writers() <= writers)
        remaining = descriptors_for(path)
    finally:
        journal.close()
        anchor.close()

    assert written
    assert later
    assert refused
    assert records == ((0, "runner.candidate_started"),)
    assert len(file_descriptors) == 1
    assert directory_descriptors == original
    assert remaining == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_link", [False, True], ids=["symlink", "hardlink"])
async def test_running_journal_releases_directory_on_unsafe_file(
    tmp_path: Path, hard_link: bool
) -> None:
    directory = private_directory(tmp_path, "attempt")
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"retained")
    foreign.chmod(0o600)
    path = directory / "journal.jsonl"
    if hard_link:
        path.hardlink_to(foreign)
    else:
        path.symlink_to(foreign)
    anchor = open_local_directory(directory)
    original = descriptors_for(directory)
    writers = live_journal_writers()
    journal = RunningJournal(anchor, SystemClock())

    try:
        refused = not await asyncio.to_thread(
            journal.append, "runner.candidate_started", {}
        )
        still_refused = not journal.append("transfer.rejected", {})
        await until(lambda: live_journal_writers() <= writers)
        directory_descriptors = descriptors_for(directory)
        foreign_descriptors = descriptors_for(foreign)
        data = foreign.read_bytes()
    finally:
        journal.close()
        anchor.close()

    assert refused
    assert still_refused
    assert directory_descriptors == original
    assert foreign_descriptors == set()
    assert data == b"retained"


@pytest.mark.asyncio
@pytest.mark.host_fixture
async def test_running_journal_releases_file_after_native_write_failure() -> None:
    directory = private_directory(
        prepared("small-filesystem"), f"{PATH_MARKER}-{os.getpid()}-journal-writer"
    )
    anchor = open_local_directory(directory)
    path = directory / "journal.jsonl"
    writers = live_journal_writers()
    journal = RunningJournal(anchor, SystemClock())
    payload: JsonObject = {"value": "a" * 131_072}

    try:
        written = await asyncio.to_thread(
            journal.append, "runner.candidate_started", {}
        )
        file_descriptors = descriptors_for(path)
        fill(directory)

        refused = not await asyncio.to_thread(
            journal.append, "transfer.materialised", payload
        )
        await until(lambda: live_journal_writers() <= writers)
        remaining = descriptors_for(path)
        (directory / "filler").unlink()
        still_refused = not journal.append("transfer.rejected", {})
        records = read_records(anchor)
    finally:
        journal.close()
        anchor.close()
        shutil.rmtree(directory)

    assert written
    assert len(file_descriptors) == 1
    assert refused
    assert remaining == set()
    assert still_refused
    assert records == ((0, "runner.candidate_started"),)


@pytest.mark.asyncio
async def test_command_start_is_durable_before_acknowledgement(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(runtime, "attempt-1", (binary("sleep"), "60"))
        await runtime.start(await runtime.submit(workload, ctx), ctx)
        runner = await running_pid(runtime, "attempt-1")
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        text = journal.read_text()
        children = child_pids(runner)
        try:
            os.kill(runner, signal.SIGKILL)
            await until(lambda: runner_pids(root) == ())
            repeated = await protocol_error(runtime.submit(workload, ctx))
        finally:
            for pid in children:
                terminate(pid)
            assert all(exited_within(pid, 10) for pid in children)

    assert text.endswith("\n")
    assert text.count("runner.candidate_started") == 1
    assert journal.read_text() == text
    assert repeated.code == "runner_unreachable"
    assert runner_pids(root) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [0, 23, -signal.SIGTERM])
async def test_command_exit_is_journalled_without_status(
    tmp_path: Path, kora_capture: Capture, expected: int
) -> None:
    forged = '{"kind":"command_exited","data":{"exit_code":72}}'
    terminal = (
        f"raise SystemExit({expected})"
        if expected >= 0
        else f"import os\nos.kill(os.getpid(), {-expected})"
    )
    script = f"print({forged!r}, flush=True)\n{terminal}"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle = await runtime.submit(
            command_workload(
                runtime, "attempt-1", (sys.executable, "-I", "-c", script)
            ),
            ctx,
        )
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"

        await runtime.start(handle, ctx)
        await until(
            lambda: any(
                event.kind == "command_exited" for event in journal_events(journal)
            )
        )
        recorded_events = journal_events(journal)
        await runtime.start(handle, ctx)
        status = await runtime.status(handle, ctx)
        repeated = journal_events(journal)

    assert [event.i for event in recorded_events] == [0, 1]
    assert [event.kind for event in recorded_events] == [
        "runner.candidate_started",
        "command_exited",
    ]
    assert recorded_events[1].data == {"exit_code": expected}
    assert repeated == recorded_events
    assert status.state == "exited"
    assert status.exit_code == expected


@pytest.mark.asyncio
async def test_command_exit_is_observed_once(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", (binary("true"),)), ctx
        )
        await runtime.start(handle, ctx)
        await until_exited(runtime, handle, ctx)
        first = await runtime.status(handle, ctx)
        second = await runtime.status(handle, ctx)
        log = attempt_directory(root, runtime, "attempt-1") / "runner.log"
        await until(lambda: command_outcomes(log) == ["started", "exited"])
        records = [
            json.loads(line)
            for line in log.read_text().splitlines()
            if '"runtime.local.command"' in line
        ]

    assert (first.state, first.phase, first.exit_code) == ("exited", "complete", 0)
    assert (second.state, second.phase, second.exit_code) == (
        first.state,
        first.phase,
        first.exit_code,
    )
    assert [record["outcome"] for record in records] == ["started", "exited"]
    assert all(isinstance(record["waited_ms"], int) for record in records)
    assert all("attempt_id" not in record for record in records)


async def until_exited(
    runtime: LocalRuntime, handle: Handle, ctx: ProtocolContext
) -> None:
    deadline = SystemClock().monotonic_ns() + 10_000_000_000
    while True:
        status = await runtime.status(handle, ctx)
        if status.state == "exited":
            return
        assert SystemClock().monotonic_ns() < deadline
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_shell_argv_execs_without_a_shell_heuristic(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, _root):
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", (binary("sh"), "-c", "exit 0")),
            ctx,
        )
        await runtime.start(handle, ctx)
        await until_exited(runtime, handle, ctx)
        status = await runtime.status(handle, ctx)

    assert (status.state, status.phase, status.exit_code) == ("exited", "complete", 0)


@pytest.mark.asyncio
async def test_rejected_workload_starts_no_process(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        agent = await runtime.submit(submitted(runtime, "attempt-agent"), ctx)
        limited = await runtime.submit(
            command_workload(
                runtime, "attempt-limited", (binary("true"),), limits={"max_events": 1}
            ),
            ctx,
        )
        agent_error = await protocol_error(runtime.start(agent, ctx))
        limited_error = await protocol_error(runtime.start(limited, ctx))
        runners = [
            await running_pid(runtime, name)
            for name in ("attempt-agent", "attempt-limited")
        ]
        assert all(child_pids(pid) == () for pid in runners)

    assert agent_error.code == "workload_kind_unavailable"
    assert limited_error.code == "workload_kind_unavailable"
    assert agent_error.retryable is False
    assert runner_pids(root) == ()


@pytest.mark.asyncio
async def test_same_submitter_reconnect_does_not_spawn(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, _root):
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", (binary("sleep"), "60")), ctx
        )
        await runtime.start(handle, ctx)
        runner = await running_pid(runtime, "attempt-1")
        await until(lambda: len(child_pids(runner)) == 1)
        first = child_pids(runner)
        await runtime.start(handle, ctx)
        second = child_pids(runner)

    assert first == second
    assert len(first) == 1


@pytest.mark.asyncio
async def test_disconnected_start_does_not_kill_the_command(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        await runtime.submit(
            command_workload(runtime, "attempt-1", (binary("sleep"), "60")), ctx
        )
        entry = await recorded(runtime, "attempt-1")
        assert isinstance(entry, Running)
        deadline = ctx.clock.monotonic_ns() + FRAME_PROGRESS_NS
        endpoint = await connected(entry.socket_path, ctx.clock, deadline)
        channel = BoundedChannel(endpoint, ctx.clock)
        request = AttachRequest(
            op="attach", request_id="wire", dispatcher_id=entry.submitter.dispatcher_id
        )

        try:
            await attested(
                endpoint,
                entry.runner,
                kernel_source(NativeIdentityReader()),
                runtime.executor,
            )
            await channel.write_typed_frame(
                FrameType.JSON,
                canonical_bytes(request.model_dump(mode="json")),
                deadline,
            )
            _, payload = await channel.read_typed_frame(
                deadline, {FrameType.JSON.value: ENVELOPE_BYTES}
            )
            read_delivery(payload, "wire", "attached")
            await channel.write_typed_frame(
                FrameType.JSON, control_request("start", "wire"), deadline
            )
            await until(lambda: len(child_pids(entry.runner.pid)) == 1)
        finally:
            channel.close()
        (leader,) = child_pids(entry.runner.pid)
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        await until(lambda: len(journal_events(journal)) == 1)
        os.kill(leader, signal.SIGTERM)
        await until(lambda: len(journal_events(journal)) == 2)
        events = journal_events(journal)

    assert [event.kind for event in events] == [
        "runner.candidate_started",
        "command_exited",
    ]
    assert events[1].data == {"exit_code": -signal.SIGTERM}


@pytest.mark.asyncio
async def test_launch_failure_has_no_fabricated_exit(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", ("/no/such/kora-command-809",)),
            ctx,
        )
        first = await protocol_error(runtime.start(handle, ctx))
        runner = await running_pid(runtime, "attempt-1")
        await until(lambda: child_pids(runner) == ())
        second = await protocol_error(runtime.start(handle, ctx))
        status = await runtime.status(handle, ctx)
        assert child_pids(runner) == ()
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        events = journal_events(journal)

    assert events == ()
    assert first.code == "command_not_started"
    assert second.code == "command_not_started"
    assert first.retryable is False
    assert status.state == "starting"
    assert status.exit_code is None


@pytest.mark.asyncio
async def test_leader_exit_capture_eof_and_descriptors_are_separate(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", (sys.executable, "-I", "-c", HOLD)),
            ctx,
        )
        await runtime.start(handle, ctx)
        runner = await running_pid(runtime, "attempt-1")
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        await until(lambda: len(journal_events(journal)) == 2)
        events = journal_events(journal)
        await until_exited(runtime, handle, ctx)
        status = await runtime.status(handle, ctx)
        holders = marker_pids(HOLD_MARKER)
        assert holders
        try:
            before = descriptor_count(runner)
        except PermissionError:
            before = None
        terminate(holders[0])
        if before is None:
            await until(lambda: marker_pids(HOLD_MARKER) == ())
        else:
            await until(lambda: descriptor_count(runner) < before)
        after = await runtime.status(handle, ctx)
        assert marker_pids(HOLD_MARKER) == ()

    assert events[-1].kind == "command_exited"
    assert events[-1].data == {"exit_code": 0}
    assert (status.state, status.exit_code) == ("exited", 0)
    assert (after.state, after.exit_code) == (status.state, status.exit_code)


@pytest.mark.asyncio
async def test_command_token_is_redacted_before_launch(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle = await runtime.submit(
            command_workload(
                runtime, "attempt-1", (sys.executable, "-I", "-c", FD_REPORT)
            ),
            ctx,
        )
        await runtime.start(handle, ctx)
        attempt = attempt_directory(root, runtime, "attempt-1")
        report = attempt / "candidate" / "fds"
        await until(lambda: report.is_file() and report.read_text() == "0 1 2")
        token = (attempt / "attempt.token").read_text()
        log = (attempt / "runner.log").read_text()
        journal = (attempt / "journal.jsonl").read_text()
        assert stat_mode(attempt / "attempt.token") == 0o600

    assert len(token) == 32
    assert token not in log
    assert token not in journal
    assert "runner.candidate_started" in journal


def command_outcomes(path: Path) -> list[str]:
    return runtime_outcomes(path, "runtime.local.command")


def runtime_outcomes(path: Path, event: str) -> list[str]:
    if not path.exists():
        return []
    found: list[str] = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != event:
            continue
        outcome = record.get("outcome")
        if isinstance(outcome, str):
            found.append(outcome)
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "resistant", "handoff", "unmarked"])
async def test_exited_command_settles_observed_descendants_without_status(
    tmp_path: Path, kora_capture: Capture, mode: str
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        command = (sys.executable, "-I", "-c", STOPPED_DESCENDANTS, "23", mode)
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", command), ctx
        )
        attempt = attempt_directory(root, runtime, "attempt-1")
        children = attempt / "candidate" / "children"
        log = attempt / "runner.log"

        await runtime.start(handle, ctx)
        await until(children.is_file)
        pids = tuple(int(pid) for pid in children.read_text().split())
        await until(lambda: bool(runtime_outcomes(log, "runtime.local.command_stop")))
        late = (children.parent / "late").read_text() if mode == "handoff" else ""
        settled = pids if not late else (*pids, int(late))
        exited = await asyncio.gather(
            *(asyncio.to_thread(exited_within, pid, 1) for pid in settled)
        )
        status = await runtime.status(handle, ctx)
        outcomes = runtime_outcomes(log, "runtime.local.command_stop")
        reader = NativeIdentityReader()
        states = tuple(reader.read(pid).state for pid in settled)

    assert len(pids) == 2
    assert len(settled) == (3 if mode == "handoff" else 2)
    if mode == "unmarked" and sys.platform == "darwin":
        assert exited == [False, True]
        assert outcomes == ["unresolved"]
    else:
        assert all(exited)
        assert outcomes == ["confirmed"]
    if sys.platform == "linux":
        assert all(state is IdentityState.ABSENT for state in states)
    assert (status.state, status.exit_code) == ("exited", 23)


@pytest.mark.asyncio
async def test_command_stop_leaves_foreign_token_lookalike_alive(
    tmp_path: Path, kora_capture: Capture
) -> None:
    token = bytes(range(16)).hex()
    canary = "private-unrelated-native-environment"
    with subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import os; os.read(0, 1)",
            f"KORA_ATTEMPT_TOKEN={token}",
        ],
        stdin=subprocess.PIPE,
        env={"KORA_ATTEMPT_TOKEN": f"{token}-other", "PRIVATE_CANARY": canary},
        start_new_session=True,
    ) as unrelated:
        try:
            async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
                handle = await runtime.submit(
                    command_workload(runtime, "attempt-1", (binary("true"),)), ctx
                )
                attempt = attempt_directory(root, runtime, "attempt-1")
                marker = attempt / "attempt.token"
                marker.write_text(token)
                marker.chmod(0o600)
                log = attempt / "runner.log"

                await runtime.start(handle, ctx)
                await until(
                    lambda: bool(runtime_outcomes(log, "runtime.local.command_stop"))
                )
                outcomes = runtime_outcomes(log, "runtime.local.command_stop")
                alive = unrelated.poll() is None
                contents = log.read_text()
        finally:
            if unrelated.stdin is not None:
                unrelated.stdin.close()
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)

    assert alive
    assert outcomes == ["confirmed"]
    assert token not in contents
    assert canary not in contents


@contextmanager
def opaque_environment_process() -> Generator[subprocess.Popen[bytes], None, None]:
    path = sys.executable
    script = "import os; os.write(1, b'r'); os.read(0, 1)"
    if sys.platform == "darwin":
        parent, name = os.path.split(path)
        candidates = (f"{parent}/{'./' * count}{name}" for count in range(4))
        path = next(
            candidate
            for candidate in candidates
            if len(os.fsencode(candidate)) % 8 in (2, 3)
        )
        script = "\n".join(
            (
                "import ctypes",
                "argv = ctypes.CDLL(None)._NSGetArgv",
                "argv.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))",
                "ctypes.c_ubyte.from_address(argv()[0][0] - 1).value = 65",
                script,
            )
        )
    with subprocess.Popen(
        [path, "-I", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={},
        start_new_session=True,
    ) as child:
        try:
            assert child.stdout is not None
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.read(1) == b"r"
            yield child
        finally:
            assert child.stdin is not None
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise


@pytest.mark.asyncio
@pytest.mark.parametrize("already_present", [True, False], ids=["before", "after"])
async def test_command_stop_confirms_only_prelaunch_opaque_processes(
    tmp_path: Path, kora_capture: Capture, already_present: bool
) -> None:
    reader = NativeIdentityReader()
    with ExitStack() as processes:
        child = (
            processes.enter_context(opaque_environment_process())
            if already_present
            else None
        )
        async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
            handle = await runtime.submit(
                command_workload(runtime, "attempt-1", (binary("true"),)), ctx
            )
            log = attempt_directory(root, runtime, "attempt-1") / "runner.log"
            if child is None:
                child = processes.enter_context(opaque_environment_process())

            member, observation = reader.command_member(
                child.pid, os.getpgrp(), b"KORA_ATTEMPT_TOKEN=synthetic"
            )
            await runtime.start(handle, ctx)
            await until(
                lambda: bool(runtime_outcomes(log, "runtime.local.command_stop"))
            )
            outcomes = runtime_outcomes(log, "runtime.local.command_stop")
            alive = child.poll() is None
    absent, final_observation = reader.command_member(
        child.pid, os.getpgrp(), b"KORA_ATTEMPT_TOKEN=synthetic"
    )

    assert alive
    assert absent is None
    assert final_observation is CommandRead.UNSTABLE
    if sys.platform == "darwin":
        assert member is None
        assert observation is CommandRead.OPAQUE
        assert outcomes == (["confirmed"] if already_present else ["unresolved"])
    else:
        assert member is not None
        assert member.owned
        assert observation is CommandRead.VERIFIED
        assert outcomes == ["confirmed"]


@pytest.mark.parametrize(
    ("observation", "current", "accepted"),
    [
        (CommandRead.OPAQUE, 201, True),
        (CommandRead.OPAQUE, 202, False),
        (CommandRead.OPAQUE, None, False),
        (CommandRead.UNSTABLE, 201, False),
    ],
    ids=["same-identity", "reused-pid", "missing-identity", "contradiction"],
)
def test_prelaunch_proof_requires_exact_identity_and_opaque_observation(
    observation: CommandRead, current: int | None, accepted: bool
) -> None:
    captured = 201

    verified = observation.matches_prelaunch(captured, current)

    assert verified is accepted


class ExitAppendClock(SystemClock):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def monotonic_ns(self) -> int:
        if threading.current_thread().name == "kora-child-reaper":
            self.calls += 1
            if self.calls == 2:
                self.entered.set()
                assert self.release.wait(15)
        return super().monotonic_ns()


def stop_workers() -> frozenset[threading.Thread]:
    names = ("kora-runtime-local", "kora-child-reaper", "kora-adopted", "kora-journal")
    return frozenset(
        thread for thread in threading.enumerate() if thread.name.startswith(names)
    )


@pytest.mark.parametrize("alignment", [4, 8])
def test_darwin_command_marker_requires_complete_environment(alignment: int) -> None:
    marker = b"KORA_ATTEMPT_TOKEN=" + bytes(range(16)).hex().encode()
    path = (2).to_bytes(4, sys.byteorder) + b"/x" + b"\0" * (alignment - 2)
    arguments = path + b"command\0\0"
    argument_only = path + b"command\0" + marker + b"\0"

    accepted = darwin_environment(
        arguments + marker + b"\0", marker, alignment=alignment
    )
    truncated = darwin_environment(
        arguments + marker + b"\0INCOMPLETE", marker, alignment=alignment
    )
    rejected = darwin_environment(argument_only, marker, alignment=alignment)

    assert accepted is True
    assert truncated is None
    assert rejected is False


@pytest.mark.parametrize(
    ("follow_up", "complete", "ambiguous"),
    [
        (CommandPIDInventory(frozenset({101})), True, False),
        (CommandPIDInventory(frozenset({101}), frozenset({101})), False, True),
        (
            CommandPIDInventory(frozenset({101}), frozenset({101}), complete=False),
            False,
            True,
        ),
        (CommandPIDInventory(frozenset({101, 102})), False, False),
        (None, False, False),
    ],
)
def test_empty_command_confirmation_preserves_follow_up_group_evidence(
    follow_up: CommandPIDInventory | None, complete: bool, ambiguous: bool
) -> None:
    first = CommandPIDInventory(frozenset({101}))

    confirmation = first.confirm_empty(follow_up)

    assert confirmation.complete is complete
    assert confirmation.ambiguous_group is ambiguous
    assert confirmation.members == ()


class CommandSignalProbe(CommandDomainStop):
    def send(self, identity: ProcessIdentity) -> bool:
        return self._signal_member(
            identity, signal.SIGTERM, self.clock.monotonic_ns() + SCAN_NS
        )


def test_command_stop_refuses_stale_identity_for_live_process() -> None:
    value = bytes(range(16)).hex()
    ready = "import os; os.write(1, b'r'); os.read(0, 1)"
    with subprocess.Popen(
        [sys.executable, "-I", "-c", ready],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={"KORA_ATTEMPT_TOKEN": value},
        start_new_session=True,
    ) as child:
        try:
            assert child.stdout is not None
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.read(1) == b"r"
            identity = identity_of(child.pid)
            stale = identity.model_copy(update={"start_time": identity.start_time + 1})
            stop = CommandSignalProbe(
                child.pid,
                f"KORA_ATTEMPT_TOKEN={value}".encode(),
                SystemClock(),
                lambda: None,
            )

            delivered = stop.send(stale)
            alive = child.poll() is None
        finally:
            if child.stdin is not None:
                child.stdin.close()
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    assert delivered is False
    assert alive


def test_census_retains_listed_group_member_after_detach() -> None:
    command = "\n".join(
        (
            "import os",
            "child = os.fork()",
            "if child:",
            "    os.waitpid(child, 0)",
            "else:",
            "    os.setsid()",
            "    os.write(1, f'{os.getpid()}\\n'.encode())",
            "    os.read(0, 1)",
        )
    )
    marker = b"KORA_ATTEMPT_TOKEN=" + bytes(range(16)).hex().encode()
    reader = NativeIdentityReader()
    with subprocess.Popen(
        [sys.executable, "-I", "-c", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={},
    ) as parent:
        pid = None
        try:
            assert parent.stdout is not None
            assert select.select([parent.stdout], [], [], 5)[0]
            pid = int(parent.stdout.readline())
            identity = identity_of(pid)

            member, observation = reader.command_member(
                pid, os.getpgrp(), marker, listed_in_group=True
            )
            unrelated, _ = reader.command_member(pid, os.getpgrp(), marker)
        finally:
            if parent.stdin is not None:
                parent.stdin.close()
            try:
                parent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if pid is not None:
                    terminate(pid)
                parent.kill()
                parent.wait(timeout=5)
                raise

    assert observation is CommandRead.VERIFIED
    assert member is not None
    assert member.process.identity == identity
    assert member.owned is False
    assert unrelated is None


class CommandCloseProbe(CommandSession):
    def stopping(self) -> bool:
        return self._stop.is_set()


@pytest.mark.asyncio
async def test_command_stop_settles_during_held_exit_append_and_cancelled_close(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = ExitAppendClock()
    scope = SecretScope(kora_capture.diagnostics)
    attempt = private_directory(tmp_path.resolve(), "attempt")
    directory = open_local_directory(attempt)
    candidate = attempt / "candidate"
    children = candidate / "children"
    journal = attempt / "journal.jsonl"
    session = CommandCloseProbe(
        directory,
        str(candidate),
        scope,
        clock,
        kora_capture.diagnostics.logger(COMPONENT),
    )
    submitter = Submitter(dispatcher_id="test", process=identity_of(os.getpid()))
    async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
        command = (sys.executable, "-I", "-c", STOPPED_DESCENDANTS, "23", "resistant")
        workload = command_workload(runtime, "attempt-1", command)
        workers = stop_workers()
        descriptors = open_descriptors()

        try:
            await asyncio.to_thread(
                session.begin, submitter, False, workload, lambda: None
            )
            session.attach_drains()
            assert await asyncio.to_thread(clock.entered.wait, 5)
            pids = tuple(int(pid) for pid in children.read_text().split())
            stopped_before_close = await asyncio.to_thread(exited_within, pids[0], 2)
            closing = asyncio.create_task(session.aclose())
            await until(session.stopping)
            retained = not scope.released
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            exited = await asyncio.gather(
                *(asyncio.to_thread(exited_within, pid, 1) for pid in pids)
            )
            still_held = not clock.release.is_set()
            records = journal_events(journal)
        finally:
            clock.release.set()
            await session.aclose()
            await until(lambda: stop_workers() <= workers)
            remaining = open_descriptors()
            directory.close()

    assert retained
    assert stopped_before_close
    assert scope.released
    assert still_held
    assert all(exited)
    assert [event.kind for event in records] == ["runner.candidate_started"]
    assert remaining == descriptors


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


@pytest.mark.asyncio
@pytest.mark.host_fixture
async def test_journal_enospc_closes_refuses_and_stops_the_candidate(
    tmp_path: Path, kora_capture: Capture
) -> None:
    small = private_directory(
        prepared("small-filesystem"), f"{PATH_MARKER}-{os.getpid()}-command"
    )
    root = private_directory(small, "state")
    ctx = runtime_context(kora_capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            workload = command_workload(runtime, "attempt-1", (binary("sleep"), "60"))
            handle = await runtime.submit(workload, ctx)
            attempt = attempt_directory(root, runtime, "attempt-1")
            candidate = attempt / "candidate"
            candidate.mkdir(mode=0o700)
            os.chmod(candidate, 0o700)
            descriptor = os.open(
                attempt / "attempt.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            os.write(descriptor, b"a" * 32)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            fill(attempt)
            runner = await running_pid(runtime, "attempt-1")
            refused = await protocol_error(runtime.start(handle, ctx))
            await until(lambda: child_pids(runner) == ())
            again = await protocol_error(runtime.start(handle, ctx))
            entry = await recorded(runtime, "attempt-1")
            journal = attempt / "journal.jsonl"
            stored = journal.read_text() if journal.exists() else ""
        finally:
            release_commands(root)
            await runtime.close()
            reap_runners(root)
            shutil.rmtree(small)

    assert refused.code == "journal_unavailable"
    assert refused.retryable is True
    assert again.code == "journal_unavailable"
    assert child_pids(runner) == ()
    assert "runner.candidate_started" not in stored
    assert "command_exited" not in stored
    assert isinstance(entry, Running)


async def empty_transfer() -> AsyncGenerator[bytes]:
    yield b""


@pytest.mark.asyncio
@pytest.mark.host_fixture
async def test_terminal_journal_enospc_fences_without_losing_exit(
    kora_capture: Capture,
) -> None:
    small = private_directory(
        prepared("small-filesystem"), f"{PATH_MARKER}-{os.getpid()}-command-exit"
    )
    root = private_directory(small, "state")
    ctx = runtime_context(kora_capture)
    item = TransferItem(
        kind="input",
        name="after-exit",
        size=0,
        sha256=f"sha256:{hashlib.sha256(b'').hexdigest()}",
    )
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            command = (sys.executable, "-I", "-c", STOPPED_DESCENDANTS, "23", "hold")
            workload = command_workload(runtime, "attempt-1", command)
            handle = await runtime.submit(workload, ctx)
            await runtime.start(handle, ctx)
            runner = await running_pid(runtime, "attempt-1")
            (leader,) = child_pids(runner)
            attempt = attempt_directory(root, runtime, "attempt-1")
            journal = attempt / "journal.jsonl"
            (started,) = journal_events(journal)
            children = attempt / "candidate" / "children"
            await until(children.is_file)
            pids = tuple(int(pid) for pid in children.read_text().split())
            fill(attempt)
            with journal.open("ab", buffering=0) as filler:
                fill_descriptor(filler.fileno())
            full_size = journal.stat().st_size

            os.kill(leader, signal.SIGTERM)
            async with asyncio.timeout(10):
                while True:
                    try:
                        await runtime.start(handle, ctx)
                    except KoraProtocolError as error:
                        start_error = error
                        break
            transfer_error = await protocol_error(
                runtime.transfer_in(handle, item, empty_transfer(), ctx)
            )
            status = await runtime.status(handle, ctx)
            anchor = open_local_directory(attempt)
            try:
                retained = read_records(anchor)
            finally:
                anchor.close()
            persisted = journal_events(journal)
            size = journal.stat().st_size
            exited = await asyncio.gather(
                *(asyncio.to_thread(exited_within, pid, 5) for pid in pids)
            )
        finally:
            release_commands(root)
            await runtime.close()
            reap_runners(root)
            shutil.rmtree(small)

    assert started.kind == "runner.candidate_started"
    assert retained == ((started.i, started.kind),)
    assert persisted == (started,)
    assert size == full_size
    assert start_error.code == "journal_unavailable"
    assert start_error.retryable is True
    assert transfer_error.code == "journal_unavailable"
    assert transfer_error.retryable is True
    assert len(pids) == 2
    assert all(exited)
    assert (status.state, status.phase, status.exit_code) == (
        "exited",
        "failed",
        -signal.SIGTERM,
    )


@pytest.mark.asyncio
async def test_controller_process_death_keeps_the_same_command(tmp_path: Path) -> None:
    root = state_root(tmp_path)
    with short_directory() as base:
        sockets = base / "sockets"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import asyncio\n"
                "from tests.plugins.runtime.local.test_command import worker\n"
                "asyncio.run(worker())\n",
            ],
            env={
                **os.environ,
                "KORA_COMMAND_ROOT": str(root),
                "KORA_COMMAND_SOCKETS": str(sockets),
            },
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            path = await asyncio.to_thread(process.stdout.readline)
            runners = runner_pids(root)
            assert len(runners) == 1
            await until(lambda: len(child_pids(runners[0])) == 1)
            leader = child_pids(runners[0])
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            identity = identity_of(runners[0])
            clock = SystemClock()
            source = kernel_source(NativeIdentityReader())
            executor = TransitionExecutor()
            try:
                observed = await runner_status(
                    path.strip(), identity, source, executor, clock, "status-1"
                )
                refused = await protocol_error(
                    start_command(
                        path.strip(),
                        identity,
                        "dispatcher-1",
                        source,
                        executor,
                        clock,
                        "start-1",
                    )
                )
                still = child_pids(runners[0])
            finally:
                await executor.close()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            release_commands(root)
            reap_runners(root)

    assert path.strip()
    assert observed.status.state == "running"
    assert refused.code == "execution_unauthorized"
    assert still == leader
