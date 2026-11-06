import asyncio
import ctypes
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.observability import LoggingSettings, configure_process
from kora.plugins.runtime.local.control import runner_status, start_command
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import COMPONENT
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.identity import Handle
from kora.protocols.runtime import Workload
from kora.protocols.time import SystemClock
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.journal import RunningJournal, read_records
from kora.runner.peers import kernel_source
from kora.runner.registry import Running, TransitionExecutor
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
        "import os, time",
        "if os.fork() == 0:",
        "    time.sleep(60)",
        "    os._exit(0)",
        "os._exit(0)",
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
        for size in (65536, 4096, 1):
            block = b"\0" * size
            while True:
                try:
                    os.write(descriptor, block)
                except OSError as error:
                    if error.errno != errno.ENOSPC:
                        raise
                    break
    finally:
        os.close(descriptor)


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
        for pid in child_pids(runner):
            terminate(pid)
        os.kill(runner, signal.SIGKILL)
        await until(lambda: runner_pids(root) == ())
        repeated = await protocol_error(runtime.submit(workload, ctx))

    assert text.endswith("\n")
    assert text.count("runner.candidate_started") == 1
    assert journal.read_text() == text
    assert repeated.code == "runner_unreachable"
    assert runner_pids(root) == ()


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
async def test_cancelled_start_does_not_kill_the_command(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, _root):
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", (binary("sleep"), "60")), ctx
        )
        runner = await running_pid(runtime, "attempt-1")
        task = asyncio.create_task(runtime.start(handle, ctx))
        await until(lambda: len(child_pids(runner)) == 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        held = child_pids(runner)
        assert len(held) == 1
        os.kill(held[0], 0)


@pytest.mark.asyncio
async def test_launch_failure_has_no_fabricated_exit(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, _root):
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

    assert first.code == "command_not_started"
    assert second.code == "command_not_started"
    assert first.retryable is False
    assert status.state == "starting"
    assert status.exit_code is None


@pytest.mark.asyncio
async def test_leader_exit_capture_eof_and_descriptors_are_separate(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, _root):
        handle = await runtime.submit(
            command_workload(runtime, "attempt-1", (sys.executable, "-I", "-c", HOLD)),
            ctx,
        )
        await runtime.start(handle, ctx)
        runner = await running_pid(runtime, "attempt-1")
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
    if not path.exists():
        return []
    found: list[str] = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != "runtime.local.command":
            continue
        outcome = record.get("outcome")
        if isinstance(outcome, str):
            found.append(outcome)
    return found


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
    assert isinstance(entry, Running)


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
