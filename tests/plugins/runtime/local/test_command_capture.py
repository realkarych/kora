import asyncio
import errno
import fcntl
import hashlib
import json
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import ComponentLogger
from kora.plugins.runtime.local.capture import (
    QUEUE_BYTES,
    SEAL_NS,
    TAIL_BYTES,
    CaptureFacts,
    CaptureIO,
    CommandCapture,
    NativeCaptureIO,
    read_log_frame,
)
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    MAX_LOG_BYTES,
    MESSAGE_PROGRESS_NS,
    SECTION_FRAME_BYTES,
    AttachRequest,
    FrameType,
    LogName,
    LogRequest,
    read_delivery,
    read_error_envelope,
    read_output_declaration,
)
from kora.plugins.runtime.local.command import CommandSession
from kora.plugins.runtime.local.command_stop import (
    CommandDomainStop,
    DomainStopFactory,
    StopOutcome,
)
from kora.plugins.runtime.local.control import attested, connected, transfer_output
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import COMPONENT
from kora.protocols.common import ProtocolContext
from kora.protocols.identity import Handle
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import Workload
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import MAX_FRAME_BYTES, BoundedChannel
from kora.runner.filesystem import DirectoryAnchor, open_local_directory
from kora.runner.identity import SCAN_NS, NativeIdentityReader
from kora.runner.native_io import observed
from kora.runner.peers import kernel_source
from kora.runner.registry import Running, Submitter, TransitionExecutor, takeover_cell
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    command_workload,
    descriptor_count,
    fill,
    launched,
    orphaned_controller,
    running_pid,
    runtime_outcomes,
)
from tests.plugins.runtime.local.test_transfer import (
    installed_deadline,
    live_descriptors,
    unlinked_spool,
)
from tests.protocols.runtime_documents import workload
from tests.support.command_diagnostics import (
    attach_event_diagnostics,
    diagnostic_number,
)
from tests.support.local_peers import CONDITION_SECONDS, identity_of, until
from tests.support.local_runners import (
    exited_within,
    short_directory,
    written,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    prepared,
    private_directory,
    protocol_error,
    recorded,
    state_root,
)

__all__ = ["kora_capture"]

HIGH_WATER_BOUND = QUEUE_BYTES + 4 * MAX_FRAME_BYTES
BLOCK = 65536
FLOOD_BLOCKS = MAX_LOG_BYTES // BLOCK + 16
SUBMITTER = Submitter(dispatcher_id="dispatcher-1", process=identity_of(os.getpid()))
HOLDER = "\n".join(
    (
        "import os, signal, sys",
        "with open('holder.pid.partial', 'w') as stream:",
        "    stream.write(str(os.getpid()))",
        "os.replace('holder.pid.partial', 'holder.pid')",
        "ready = int(sys.argv[1])",
        "os.write(ready, b'r')",
        "os.close(ready)",
        "signal.pause()",
    )
)
ESCAPING = "\n".join(
    (
        "import os, sys",
        f"holder = {HOLDER!r}",
        "reading, writing = os.pipe()",
        "if os.fork() == 0:",
        "    os.close(reading)",
        "    os.setsid()",
        "    os.set_inheritable(writing, True)",
        "    argv = [sys.executable, '-I', '-c', holder, str(writing)]",
        "    os.execve(sys.executable, argv, {})",
        "os.close(writing)",
        "assert os.read(reading, 1) == b'r'",
        "os.write(1, b'leader')",
    )
)
FLOODING = "\n".join(
    (
        "import fcntl, os",
        "os.write(1, b'leader')",
        "if os.fork() == 0:",
        "    block = b'f' * 65536",
        "    while True:",
        "        os.write(1, block)",
        "with open({gate}, 'rb') as gate:",
        "    fcntl.flock(gate, fcntl.LOCK_SH)",
    )
)
BOUNDED = "\n".join(
    (
        "import os",
        "token = os.environ['KORA_ATTEMPT_TOKEN'].encode()",
        "os.write(1, b'x' + token[:16])",
        "with open('go', 'rb') as gate:",
        "    gate.read()",
        "os.write(1, token[16:] + b'\\xff')",
        "block = b'z' * 65536",
        f"for _ in range({FLOOD_BLOCKS}):",
        "    os.write(1, block)",
        "    os.write(2, block)",
    )
)
SANITIZED_PREFIX = b"x[redacted]\xef\xbf\xbd"


class OffsetClock(SystemClock):
    def __init__(self) -> None:
        self.offset_ns = 0
        self.sleepers: list[asyncio.Future[None]] = []

    def monotonic_ns(self) -> int:
        return time.monotonic_ns() + self.offset_ns

    async def sleep_until(self, deadline_ns: int) -> None:
        loop = asyncio.get_running_loop()
        while (remaining := deadline_ns - self.monotonic_ns()) > 0:
            woken: asyncio.Future[None] = loop.create_future()
            self.sleepers.append(woken)
            try:
                await asyncio.wait({woken}, timeout=remaining / 1_000_000_000)
            finally:
                self.sleepers.remove(woken)

    def advance(self, nanoseconds: int) -> None:
        self.offset_ns += nanoseconds
        for woken in self.sleepers:
            if not woken.done():
                woken.set_result(None)


class HeldIO(NativeCaptureIO):
    def __init__(self, held: str | None) -> None:
        self.held = held
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes = 0
        self.sync_error: OSError | None = None

    def _hold(self, operation: str) -> None:
        if self.held == operation:
            self.entered.set()
            if not self.release.wait(CONDITION_SECONDS * 3):
                raise TimeoutError("the held capture operation was not released")

    def write(self, descriptor: int, data: memoryview) -> int:
        self._hold("write")
        written = super().write(descriptor, data)
        self.writes += 1
        return written

    def sync(self, descriptor: int) -> None:
        self._hold("sync")
        if self.sync_error is not None:
            raise self.sync_error
        super().sync(descriptor)


class CaptureProbe(CommandSession):
    pipes: tuple[tuple[int, tuple[int, int]], ...] = ()

    def __init__(
        self,
        attempt: DirectoryAnchor,
        candidate: str,
        scope: SecretScope,
        clock: Clock,
        log: ComponentLogger,
        capture_io: CaptureIO | None,
        stopper: DomainStopFactory,
    ) -> None:
        self.reaper = AdoptedReaper()
        super().__init__(
            attempt, candidate, scope, clock, log, self.reaper, capture_io, stopper
        )

    async def stop(self) -> None:
        try:
            await super().stop()
        finally:
            await self.reaper.stop()

    def attach_drains(self) -> None:
        self.pipes = tuple(
            (descriptor, identity_at(descriptor))
            for descriptor in (self._stdout, self._stderr)
            if descriptor is not None
        )
        super().attach_drains()

    def capture(self) -> CommandCapture:
        assert self._capture is not None
        return self._capture

    def stop_outcome(self) -> StopOutcome | None:
        task = self._domain_stop
        return None if task is None or not task.done() else task.result()

    def sealing(self) -> bool:
        return self._capture_seal is not None

    def stop_settled(self) -> bool:
        seal = self._capture_seal
        return self.stop_outcome() is not None and (seal is None or seal.done())

    def stopping(self) -> bool:
        return self._stop.is_set()

    def leader(self) -> int | None:
        return None if self._owner is None else self._owner.pid

    def diagnostic_owner(self) -> JsonObject:
        owner = self._owner
        if owner is None:
            return {"state": "absent"}
        if not owner.lock.acquire(blocking=False):
            return {"state": "unavailable"}
        try:
            return {
                "state": "observed",
                "exit_observed": owner.status is not None,
                "reaped": owner.reaped,
                "error_recorded": owner.error is not None,
            }
        finally:
            owner.lock.release()

    def diagnostic_snapshot(self) -> JsonObject:
        domain = task_state(self._domain_stop)
        if domain == "finished" and self._domain_stop is not None:
            domain = (
                "confirmed"
                if self._domain_stop.result() is StopOutcome.CONFIRMED
                else "unresolved"
            )
        capture = self._capture
        return {
            "owner": self.diagnostic_owner(),
            "domain_stop": domain,
            "capture_seal": task_state(self._capture_seal),
            "capture": {"state": "absent"}
            if capture is None
            else {
                "state": "attached",
                "ended": capture.ended,
                "writer_alive": capture.writer.is_alive(),
                "stdout_observed_bytes": diagnostic_number(capture.observed("stdout")),
                "stderr_observed_bytes": diagnostic_number(capture.observed("stderr")),
            },
        }

    async def settle_capture(self) -> None:
        await until(self.sealing)
        task = self._capture_seal
        assert task is not None
        async with asyncio.timeout(CONDITION_SECONDS * 3):
            await asyncio.shield(task)


def task_state(task: asyncio.Task[object] | None) -> str:
    if task is None:
        return "absent"
    if not task.done():
        return "pending"
    if task.cancelled():
        return "cancelled"
    return "finished" if task.exception() is None else "failed"


def identity_at(descriptor: int) -> tuple[int, int]:
    information = os.fstat(descriptor)
    return information.st_dev, information.st_ino


def replaced_or_closed(descriptor: int, identity: tuple[int, int]) -> bool:
    try:
        return identity_at(descriptor) != identity
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        return True


def capture_workload(script: str) -> Workload:
    document = workload(
        kind="command",
        plan_executor="runner",
        publish=None,
        session_spec=None,
        env={},
        secret_refs={},
        harness_credentials={},
        artifact_inputs={},
        resources=[],
        limits={},
        deadline=None,
        resolved_config={
            "cmd": [sys.executable, "-I", "-c", script],
            "workspace": {"repo": "local"},
        },
    )
    return Workload.model_validate_json(json.dumps(document))


@asynccontextmanager
async def capture_session(
    parent: Path,
    capture: Capture,
    script: str,
    *,
    clock: SystemClock | None = None,
    io: HeldIO | None = None,
    gate: bool = False,
    stopper: DomainStopFactory = CommandDomainStop,
    existing: bool = False,
    attach_after_stop: bool = False,
) -> AsyncGenerator[tuple[CaptureProbe, Path]]:
    attempt = private_directory(parent, f"{PATH_MARKER}-{os.getpid()}-capture")
    candidate = attempt / "candidate"
    candidate.mkdir(mode=0o700)
    if gate:
        os.mkfifo(candidate / "go", 0o600)
    if existing:
        (attempt / "stdout.log").write_bytes(b"planted")
    anchor = open_local_directory(attempt)
    session_clock = clock or SystemClock()
    session = CaptureProbe(
        anchor,
        str(candidate),
        SecretScope(capture.diagnostics),
        session_clock,
        capture.diagnostics.logger(COMPONENT),
        io,
        stopper,
    )
    work = capture_workload(script)
    prelaunch: dict[int, int] = {}
    try:
        if sys.platform == "darwin":
            await asyncio.to_thread(
                session.capture_prelaunch,
                prelaunch,
                work,
                session_clock.monotonic_ns() + SCAN_NS,
            )
        session.accept(SUBMITTER, prelaunch, work)
        await asyncio.to_thread(session.supervise, SUBMITTER, False, work, lambda: None)
        if attach_after_stop:
            await until(session.stop_settled)
        session.attach_drains()
        yield session, attempt
    finally:
        if io is not None:
            io.release.set()
        await session.stop()
        anchor.close()


def capture_records(capture: Capture) -> list[tuple[object, object, object]]:
    return [
        (item.get("outcome"), item.get("retained_bytes"), item.get("observed_bytes"))
        for item in capture.finish()
        if item.get("event") == "runtime.local.capture"
    ]


def cap_records(attempt: Path) -> list[object]:
    found: list[object] = []
    for line in (attempt / "journal.jsonl").read_bytes().splitlines():
        record = json.loads(line)
        if record["kind"] == "runner.cap_reached":
            found.append(record["data"])
    return found


def facts_of(session: CaptureProbe, which: LogName) -> CaptureFacts:
    facts = session.capture().facts(which)
    assert facts is not None
    return facts


def stream_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


async def advance_past_seal(
    session: CaptureProbe, clock: OffsetClock, *, ended: bool = True
) -> None:
    if ended:
        await until(lambda: session.capture().ended)
    await until(lambda: session.sealing() and bool(clock.sleepers))
    clock.advance(SEAL_NS + 1)


def capture_threads() -> int:
    return sum(thread.name == "kora-capture" for thread in threading.enumerate())


@pytest.mark.asyncio
async def test_capture_diagnostics_preserve_failure_without_private_content(
    tmp_path: Path, kora_capture: Capture
) -> None:
    private_content = "capture-private-marker"
    io = HeldIO("write")
    failure = AssertionError("capture wait did not finish")
    async with capture_session(
        tmp_path, kora_capture, f"print({private_content!r})", io=io
    ) as (session, _attempt):
        await until(io.entered.is_set)
        await until(session.sealing)

        snapshot = session.diagnostic_snapshot()
        failure.add_note(json.dumps({"pre_teardown": snapshot}, sort_keys=True))
        io.release.set()
        await session.settle_capture()
    attach_event_diagnostics(failure, kora_capture)

    assert str(failure) == "capture wait did not finish"
    assert snapshot["owner"] == {
        "state": "observed",
        "exit_observed": True,
        "reaped": True,
        "error_recorded": False,
    }
    assert snapshot["domain_stop"] == "confirmed"
    assert snapshot["capture_seal"] == "pending"
    assert private_content not in "".join(failure.__notes__)
    assert len(failure.__notes__) == 2
    assert "post_teardown_events" in failure.__notes__[1]


@pytest.mark.asyncio
async def test_command_captures_are_sanitized_and_bounded(
    tmp_path: Path, kora_capture: Capture
) -> None:
    flooded = FLOOD_BLOCKS * BLOCK
    async with capture_session(tmp_path, kora_capture, BOUNDED, gate=True) as (
        session,
        attempt,
    ):
        token = (attempt / "attempt.token").read_bytes()
        await until(lambda: session.capture().observed("stdout") >= 1)
        go = attempt / "candidate" / "go"
        await asyncio.to_thread(lambda: os.close(os.open(go, os.O_WRONLY)))
        await session.settle_capture()
        stdout = facts_of(session, "stdout")
        stderr = facts_of(session, "stderr")
        retained = (attempt / "stdout.log").read_bytes()
        retained_errors = (attempt / "stderr.log").read_bytes()
        caps = cap_records(attempt)
    records = capture_records(kora_capture)

    observed = len(SANITIZED_PREFIX) + flooded
    assert retained[: len(SANITIZED_PREFIX)] == SANITIZED_PREFIX
    assert retained.count(b"z") == MAX_LOG_BYTES - len(SANITIZED_PREFIX)
    assert token not in retained
    assert len(retained) == MAX_LOG_BYTES
    assert (stdout.size, stdout.observed_bytes) == (MAX_LOG_BYTES, observed)
    assert (stdout.eof, stdout.truncated) == (True, True)
    assert stdout.payload_digest == stream_digest(retained)
    assert stdout.tail == retained[-TAIL_BYTES:]
    assert retained_errors == b"z" * MAX_LOG_BYTES
    assert (stderr.size, stderr.observed_bytes) == (MAX_LOG_BYTES, flooded)
    assert (stderr.eof, stderr.truncated) == (True, True)
    assert stderr.payload_digest == stream_digest(retained_errors)
    assert stderr.tail == retained_errors[-TAIL_BYTES:]
    assert caps == [
        {"kind": "stdout", "omitted": observed - MAX_LOG_BYTES},
        {"kind": "stderr", "omitted": flooded - MAX_LOG_BYTES},
    ]
    assert session.capture().high_water <= HIGH_WATER_BOUND
    assert records == [
        ("truncated", MAX_LOG_BYTES, observed),
        ("truncated", MAX_LOG_BYTES, flooded),
    ]


@pytest.mark.asyncio
async def test_command_capture_without_eof_is_truncated(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = OffsetClock()
    holder: int | None = None
    try:
        async with capture_session(tmp_path, kora_capture, ESCAPING, clock=clock) as (
            session,
            attempt,
        ):
            pid_file = attempt / "candidate" / "holder.pid"
            await until(pid_file.is_file)
            holder = int(pid_file.read_text())
            await until(lambda: session.stop_outcome() is not None)
            outcome = session.stop_outcome()
            await advance_past_seal(session, clock, ended=False)
            await session.settle_capture()
            facts = facts_of(session, "stdout")
            errors = facts_of(session, "stderr")
            closed = all(
                replaced_or_closed(descriptor, identity)
                for descriptor, identity in session.pipes
            )
            os.kill(holder, 0)
            retained = (attempt / "stdout.log").read_bytes()
    finally:
        if holder is not None:
            os.kill(holder, signal.SIGKILL)
            assert exited_within(holder, 10)
    records = capture_records(kora_capture)

    assert outcome is StopOutcome.CONFIRMED
    assert closed
    assert retained == b"leader"
    assert (facts.size, facts.observed_bytes) == (6, 6)
    assert (facts.eof, facts.truncated) == (False, True)
    assert (errors.eof, errors.truncated) == (False, True)
    assert facts.payload_digest == stream_digest(b"leader")
    assert [record[0] for record in records] == ["truncated", "truncated"]


async def release_flooding_leader(session: CaptureProbe, gate: int) -> None:
    fcntl.flock(gate, fcntl.LOCK_UN)
    stopping = asyncio.create_task(until(lambda: session.stop_outcome() is not None))
    cancelled = await observed(stopping)
    stopping.result()
    if cancelled:
        raise asyncio.CancelledError


async def cancelled_leader_release(session: CaptureProbe, gate: int) -> None:
    current = asyncio.current_task()
    assert current is not None
    current.cancel()
    await release_flooding_leader(session, gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_release", [False, True])
async def test_command_capture_never_delays_stop(
    tmp_path: Path, kora_capture: Capture, cancel_release: bool
) -> None:
    io = HeldIO("write")
    gate_path = tmp_path / "leader-gate"
    script = FLOODING.format(gate=repr(str(gate_path)))
    with gate_path.open("wb") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        async with capture_session(tmp_path, kora_capture, script, io=io) as (
            session,
            attempt,
        ):
            try:
                await until(io.entered.is_set)
            finally:
                if cancel_release:
                    releasing = asyncio.create_task(
                        cancelled_leader_release(session, gate.fileno())
                    )
                    with pytest.raises(asyncio.CancelledError):
                        await releasing
                else:
                    await release_flooding_leader(session, gate.fileno())
            outcome = session.stop_outcome()
            entered = io.entered.is_set()
            writes_at_stop = io.writes
            io.release.set()
            await session.settle_capture()
            facts = facts_of(session, "stdout")
            retained = (attempt / "stdout.log").read_bytes()
            high_water = session.capture().high_water

    assert outcome is StopOutcome.CONFIRMED
    assert entered
    assert writes_at_stop == 0
    assert facts.eof
    assert facts.size == len(retained)
    assert facts.payload_digest == stream_digest(retained)
    assert high_water <= HIGH_WATER_BOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["write", "sync"])
@pytest.mark.parametrize("moment", ["seal", "shutdown"])
async def test_command_capture_seal_is_bounded(
    tmp_path: Path, kora_capture: Capture, held: str, moment: str
) -> None:
    clock = OffsetClock()
    io = HeldIO(held)
    script = "import os; os.write(1, b'out'); os.write(2, b'err')"
    async with capture_session(tmp_path, kora_capture, script, clock=clock, io=io) as (
        session,
        _attempt,
    ):
        capture = session.capture()
        await until(io.entered.is_set)
        await until(lambda: session.stop_outcome() is StopOutcome.CONFIRMED)
        closing: asyncio.Task[None] | None = None
        if moment == "shutdown":
            closing = asyncio.create_task(session.stop())
            await until(session.stopping)
        await advance_past_seal(session, clock)
        if closing is None:
            await session.settle_capture()
        else:
            async with asyncio.timeout(CONDITION_SECONDS):
                await closing
        writer_held = capture.writer.is_alive()
        facts = (capture.facts("stdout"), capture.facts("stderr"))
        high_water = capture.high_water
        io.release.set()
        await until(lambda: not capture.writer.is_alive())
    records = capture_records(kora_capture)

    assert writer_held
    assert facts == (None, None)
    assert high_water <= HIGH_WATER_BOUND
    assert [record[0] for record in records] == ["unsealed", "unsealed"]


async def log_bytes(
    runtime: LocalRuntime,
    handle: Handle,
    ctx: ProtocolContext,
    which: LogName,
    offset: int,
) -> tuple[tuple[int, ...], bytes]:
    parts: list[bytes] = []
    async for chunk in runtime.read_log(handle, which, offset, ctx):
        parts.append(chunk)
    return tuple(len(part) for part in parts), b"".join(parts)


async def attached_channel(entry: Running, ctx: ProtocolContext) -> BoundedChannel:
    clock = ctx.clock
    deadline = clock.monotonic_ns() + MESSAGE_PROGRESS_NS
    endpoint = await connected(entry.socket_path, clock, deadline)
    channel = BoundedChannel(endpoint, clock)
    executor = TransitionExecutor(1, 1)
    try:
        await attested(
            endpoint, entry.runner, kernel_source(NativeIdentityReader()), executor
        )
        request = AttachRequest(
            op="attach",
            request_id="attach-1",
            dispatcher_id=entry.submitter.dispatcher_id,
        )
        await channel.write_typed_frame(
            FrameType.JSON, canonical_bytes(request.model_dump(mode="json")), deadline
        )
        reservations = {FrameType.JSON.value: ENVELOPE_BYTES}
        _, payload = await channel.read_typed_frame(deadline, reservations)
        read_delivery(payload, "attach-1", "attached")
    except BaseException:
        channel.close()
        raise
    finally:
        await executor.close()
    return channel


async def disconnect_mid_stream(entry: Running, ctx: ProtocolContext) -> int:
    clock = ctx.clock
    deadline = clock.monotonic_ns() + MESSAGE_PROGRESS_NS
    channel = await attached_channel(entry, ctx)
    try:
        request = LogRequest(
            op="read_log", request_id="log-1", which="stdout", from_offset=0
        )
        await channel.write_typed_frame(
            FrameType.JSON, canonical_bytes(request.model_dump(mode="json")), deadline
        )
        reservations = {
            FrameType.JSON.value: ENVELOPE_BYTES,
            FrameType.BYTES.value: SECTION_FRAME_BYTES,
        }
        _, payload = await channel.read_typed_frame(deadline, reservations)
        declared = read_output_declaration(payload, "log-1")
        kind, first = await channel.read_typed_frame(deadline, reservations)
        assert kind == FrameType.BYTES
        assert len(first) == SECTION_FRAME_BYTES
        return declared.size
    finally:
        channel.close()


def runner_descriptors(pid: int) -> int | None:
    try:
        return descriptor_count(pid)
    except PermissionError:
        return None


def log_payload(which: LogName) -> bytes:
    unit = b"o" if which == "stdout" else b"e"
    return (unit * 127 + b"\n") * 1100


LOGGING = "\n".join(
    (
        "import os, sys",
        "out = (b'o' * 127 + b'\\n') * 1100",
        "err = (b'e' * 127 + b'\\n') * 1100",
        "sys.stdout.buffer.write(out)",
        "sys.stderr.buffer.write(err)",
    )
)


def captures_logged(attempt: Path) -> bool:
    return len(runtime_outcomes(attempt / "runner.log", "runtime.local.capture")) == 2


async def read_log_as_observer(
    path: str, runner: ProcessIdentity, executor: TransitionExecutor, base: Path
) -> object:
    clock = SystemClock()
    descriptor = os.open(
        base / "spool", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
    )
    os.unlink(base / "spool")
    try:
        return await transfer_output(
            path,
            runner,
            "dispatcher-2",
            kernel_source(NativeIdentityReader()),
            executor,
            clock,
            descriptor,
            LogRequest(
                op="read_log", request_id="log-1", which="stdout", from_offset=0
            ),
            clock.monotonic_ns() + MESSAGE_PROGRESS_NS,
            whole=True,
            close_spool=lambda: None,
        )
    finally:
        os.close(descriptor)


ObserverRequest = Callable[
    [str, ProcessIdentity, TransitionExecutor, Path], Awaitable[object]
]


async def observational_refusal(
    tmp_path: Path, ask: ObserverRequest = read_log_as_observer
) -> str:
    (tmp_path / "observer").mkdir()
    root = state_root(tmp_path / "observer")
    with short_directory() as base:
        async with orphaned_controller(
            root, base / "sockets", start=False
        ) as controller:
            executor = TransitionExecutor()
            try:
                refused = await protocol_error(
                    ask(controller.path.strip(), controller.orphan(), executor, base)
                )
            finally:
                await executor.close()
    return refused.code


class UnresolvedStop:
    def __init__(
        self,
        group: int,
        marker: bytes,
        clock: Clock,
        wake_reaper: Callable[[], None],
        prelaunch: dict[int, int] | None,
    ) -> None:
        self.group = group

    def stop(self, started_ns: int) -> StopOutcome:
        try:
            os.killpg(self.group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return StopOutcome.UNRESOLVED


async def session_refusal(session: CaptureProbe) -> str:
    refused = await protocol_error(
        asyncio.to_thread(session.open_log, SUBMITTER, "stdout", 0)
    )
    return refused.code


async def refused_session_reads(
    parent: Path, capture: Capture
) -> tuple[str, str, str, CaptureFacts]:
    clock = OffsetClock()
    io = HeldIO("sync")
    script = "print('read')"
    unsealed_parent = private_directory(parent, "unsealed")
    async with capture_session(
        unsealed_parent, capture, script, clock=clock, io=io
    ) as (session, _attempt):
        await advance_past_seal(session, clock)
        await session.settle_capture()
        unsealed = await session_refusal(session)
    unresolved_parent = private_directory(parent, "unresolved")
    async with capture_session(
        unresolved_parent, capture, script, stopper=UnresolvedStop
    ) as (session, _attempt):
        await until(lambda: session.stop_outcome() is StopOutcome.UNRESOLVED)
        unresolved = await session_refusal(session)
    planted_parent = private_directory(parent, "planted")
    async with capture_session(planted_parent, capture, script, existing=True) as (
        session,
        attempt,
    ):
        await session.settle_capture()
        planted_facts = facts_of(session, "stdout")
        planted = await session_refusal(session)
        assert (attempt / "stdout.log").read_bytes() == b"planted"
    return unsealed, unresolved, planted, planted_facts


async def superseded_read(
    runtime: LocalRuntime, ctx: ProtocolContext, entry: Running
) -> str:
    successor = entry.submitter.model_copy(update={"dispatcher_id": "dispatcher-2"})
    channel = await attached_channel(entry, ctx)
    try:
        await runtime.transition(
            runtime.registry.key("attempt-1"),
            lambda current: takeover_cell(current, entry, successor, False),
            ctx,
            "submit",
        )
        request = LogRequest(
            op="read_log", request_id="log-2", which="stderr", from_offset=0
        )
        deadline = ctx.clock.monotonic_ns() + MESSAGE_PROGRESS_NS
        await channel.write_typed_frame(
            FrameType.JSON, canonical_bytes(request.model_dump(mode="json")), deadline
        )
        _, payload = await channel.read_typed_frame(
            deadline, {FrameType.JSON.value: ENVELOPE_BYTES}
        )
        _, error = read_error_envelope(payload)
        return error.code
    finally:
        channel.close()


@pytest.mark.asyncio
async def test_read_log_streams_retained_bytes(
    tmp_path: Path, kora_capture: Capture
) -> None:
    out = log_payload("stdout")
    err = log_payload("stderr")
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle = await runtime.submit(
            command_workload(
                runtime, "attempt-1", (sys.executable, "-I", "-c", LOGGING)
            ),
            ctx,
        )
        pending = await protocol_error(
            anext(runtime.read_log(handle, "stdout", 0, ctx))
        )
        attempt = attempt_directory(root, runtime, "attempt-1")
        await runtime.start(handle, ctx)
        await until(lambda: captures_logged(attempt))
        (out_frames, out_bytes), (err_frames, err_bytes) = await asyncio.gather(
            log_bytes(runtime, handle, ctx, "stdout", 0),
            log_bytes(runtime, handle, ctx, "stderr", 0),
        )
        _, suffix = await log_bytes(runtime, handle, ctx, "stdout", 70_001)
        _, empty = await log_bytes(runtime, handle, ctx, "stdout", len(out))
        beyond = await protocol_error(
            log_bytes(runtime, handle, ctx, "stdout", len(out) + 1)
        )
        negative = await protocol_error(log_bytes(runtime, handle, ctx, "stdout", -1))
        pid = await running_pid(runtime, "attempt-1")
        entry = await recorded(runtime, "attempt-1")
        assert isinstance(entry, Running)
        runner_before = runner_descriptors(pid)
        declared = await disconnect_mid_stream(entry, ctx)
        if runner_before is not None:
            await until(lambda: runner_descriptors(pid) == runner_before)
        _, after_disconnect = await log_bytes(runtime, handle, ctx, "stdout", 0)
        before = live_descriptors()
        os.kill(pid, signal.SIGSTOP)
        task = asyncio.create_task(log_bytes(runtime, handle, ctx, "stderr", 0))
        try:
            await until(lambda: unlinked_spool(before))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            released = live_descriptors()
        finally:
            os.kill(pid, signal.SIGCONT)
        await installed_deadline(runtime, handle, ctx, -60)
        transfer_expired = await protocol_error(
            anext(runtime.transfer_out(handle, "missing", ctx))
        )
        _, past_deadline = await log_bytes(runtime, handle, ctx, "stderr", 0)
        current = await recorded(runtime, "attempt-1")
        assert isinstance(current, Running)
        closed = current.model_copy(update={"gate_closed": True})
        await written(runtime, "attempt-1", closed, ctx)
        _, gate_closed = await log_bytes(runtime, handle, ctx, "stderr", 0)
        log = attempt / "stdout.log"
        with log.open("r+b") as stream:
            stream.seek(1)
            stream.write(b"!")
        altered: list[bytes] = []

        async def altered_read(offset: int) -> None:
            async for chunk in runtime.read_log(handle, "stdout", offset, ctx):
                altered.append(chunk)

        mismatch = await protocol_error(altered_read(0))
        offset_mismatch = await protocol_error(altered_read(70_001))
        reopened = closed.model_copy(update={"deadline": None, "gate_closed": False})
        await written(runtime, "attempt-1", reopened, ctx)
        superseded = await superseded_read(runtime, ctx, reopened)
    observer = await observational_refusal(tmp_path)
    unsealed, unresolved, planted, planted_facts = await refused_session_reads(
        tmp_path, kora_capture
    )
    records = capture_records(kora_capture)

    assert pending.code == "workload_busy"
    assert pending.retryable
    assert out_bytes == out
    assert err_bytes == err
    assert all(0 < size <= SECTION_FRAME_BYTES for size in (*out_frames, *err_frames))
    assert len(out_frames) == -(-len(out) // SECTION_FRAME_BYTES)
    assert suffix == out[70_001:]
    assert empty == b""
    assert beyond.code == "transfer_refused"
    assert negative.code == "transfer_refused"
    assert released == before
    assert declared == len(out)
    assert after_disconnect == out
    assert mismatch.code == "integrity_error"
    assert offset_mismatch.code == "integrity_error"
    assert altered == []
    assert observer == "execution_unauthorized"
    assert transfer_expired.code == "deadline_expired"
    assert past_deadline == err
    assert gate_closed == err
    assert superseded == "submitter_superseded"
    assert unsealed == "transfer_refused"
    assert unresolved == "transfer_refused"
    assert planted == "transfer_refused"
    assert (planted_facts.size, planted_facts.identity) == (0, None)
    assert [record[0] for record in records] == [
        "unsealed",
        "unsealed",
        "unsealed",
        "unsealed",
        "write_failed",
        "complete",
    ]


@pytest.mark.asyncio
@pytest.mark.host_fixture
async def test_command_capture_on_full_disk(kora_capture: Capture) -> None:
    parent = private_directory(
        prepared("small-filesystem"), f"{PATH_MARKER}-{os.getpid()}-capture-disk"
    )
    script = "\n".join(
        (
            "import os",
            "with open('go', 'rb') as gate:",
            "    gate.read()",
            "block = b'd' * 65536",
            "for _ in range(16):",
            "    os.write(1, block)",
        )
    )
    observed_failure: AssertionError | TimeoutError | None = None
    try:
        async with capture_session(parent, kora_capture, script, gate=True) as (
            session,
            attempt,
        ):
            fill(attempt)
            filler = attempt / "filler"
            with filler.open("r+b") as opened:
                opened.truncate(filler.stat().st_size - 262144)
            go = attempt / "candidate" / "go"
            await asyncio.to_thread(lambda: os.close(os.open(go, os.O_WRONLY)))
            try:
                await session.settle_capture()
            except (AssertionError, TimeoutError) as error:
                observed_failure = error
                error.add_note(
                    json.dumps(
                        {"pre_teardown": session.diagnostic_snapshot()}, sort_keys=True
                    )
                )
                raise
            facts = facts_of(session, "stdout")
            on_disk = (attempt / "stdout.log").read_bytes()
            opened_log = await asyncio.to_thread(
                session.open_log, SUBMITTER, "stdout", 0
            )
            try:
                frames = [
                    read_log_frame(
                        opened_log,
                        position,
                        min(SECTION_FRAME_BYTES, opened_log.size - position),
                    )
                    for position in range(0, opened_log.size, SECTION_FRAME_BYTES)
                ]
            finally:
                os.close(opened_log.descriptor)
            filler.unlink()
        io = HeldIO(None)
        io.sync_error = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        sync_parent = private_directory(parent, "sync")
        async with capture_session(
            sync_parent, kora_capture, "print('synced')", io=io
        ) as (sync_session, _sync_attempt):
            try:
                await sync_session.settle_capture()
            except (AssertionError, TimeoutError) as error:
                observed_failure = error
                error.add_note(
                    json.dumps(
                        {"pre_teardown": sync_session.diagnostic_snapshot()},
                        sort_keys=True,
                    )
                )
                raise
            unsealed = sync_session.capture().facts("stdout")
    except (AssertionError, TimeoutError) as error:
        if error is observed_failure:
            attach_event_diagnostics(error, kora_capture)
        raise
    finally:
        shutil.rmtree(parent)
    records = capture_records(kora_capture)

    assert 0 < facts.size < 16 * 65536
    assert facts.observed_bytes == 16 * 65536
    assert facts.truncated
    assert facts.size == len(on_disk)
    assert facts.payload_digest == stream_digest(on_disk)
    assert facts.tail == on_disk[-TAIL_BYTES:]
    assert b"".join(frames) == on_disk
    assert unsealed is None
    assert [record[0] for record in records] == [
        "write_failed",
        "complete",
        "unsealed",
        "unsealed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["success", "failure", "cancellation", "disconnect", "shutdown"]
)
async def test_command_capture_releases_resources(
    tmp_path: Path, kora_capture: Capture, case: str
) -> None:
    if case == "disconnect":
        await released_after_disconnect(tmp_path, kora_capture)
        return
    scripts = {
        "success": "print('ok')",
        "failure": "import sys; sys.stderr.write('bad'); sys.exit(3)",
        "cancellation": "print('cancelled')",
        "shutdown": "import signal; print('held', flush=True); signal.pause()",
    }
    descriptors = live_descriptors()
    threads = capture_threads()
    clock = OffsetClock()
    io = HeldIO("sync" if case == "cancellation" else None)
    leader: int | None = None
    async with capture_session(
        tmp_path, kora_capture, scripts[case], clock=clock, io=io
    ) as (session, _attempt):
        capture = session.capture()
        leader = session.leader()
        if case == "shutdown":
            await until(lambda: capture.observed("stdout") > 0)
        elif case == "cancellation":
            await until(lambda: session.capture().ended)
            await until(lambda: session.sealing() and bool(clock.sleepers))
            closing = asyncio.create_task(session.stop())
            await until(session.stopping)
            closing.cancel()
            io.release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await session.settle_capture()
    if leader is not None and case == "shutdown":
        os.kill(leader, signal.SIGKILL)
    await until(lambda: capture_threads() == threads)
    await until(lambda: live_descriptors() <= descriptors)
    records = capture_records(kora_capture)

    assert capture.queued == 0
    assert capture.settled
    assert len(records) == 2
    if case == "shutdown":
        assert [record[0] for record in records] == ["unsealed", "unsealed"]
    else:
        assert all(record[0] == "complete" for record in records)


@pytest.mark.asyncio
async def test_command_capture_seals_when_the_domain_stops_before_drains_attach(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with capture_session(
        tmp_path, kora_capture, "print('ok')", attach_after_stop=True
    ) as (session, _attempt):
        stopped = session.stop_outcome()

        await session.settle_capture()
        facts = facts_of(session, "stdout")
    records = capture_records(kora_capture)

    assert stopped is StopOutcome.CONFIRMED
    assert [record[0] for record in records] == ["complete", "complete"]
    assert facts.eof
    assert facts.size == len(b"ok\n")


async def released_after_disconnect(tmp_path: Path, capture: Capture) -> None:
    async with launched(tmp_path, capture) as (runtime, ctx, root):
        handle = await runtime.submit(
            command_workload(
                runtime, "attempt-1", (sys.executable, "-I", "-c", LOGGING)
            ),
            ctx,
        )
        attempt = attempt_directory(root, runtime, "attempt-1")
        await runtime.start(handle, ctx)
        await until(lambda: captures_logged(attempt))
        entry = await recorded(runtime, "attempt-1")
        assert isinstance(entry, Running)
        pid = entry.runner.pid
        settled = runner_descriptors(pid)
        before = live_descriptors()
        declared = await disconnect_mid_stream(entry, ctx)
        if settled is not None:
            await until(lambda: runner_descriptors(pid) == settled)
        _, body = await log_bytes(runtime, handle, ctx, "stdout", 0)
        after = live_descriptors()

    assert declared == len(log_payload("stdout"))
    assert body == log_payload("stdout")
    assert after == before
