import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    FrameType,
    control_request,
    read_delivery,
    read_quiescence,
    read_status,
)
from kora.plugins.runtime.local.command import COMPONENT
from kora.plugins.runtime.local.command_stop import (
    CommandDomainStop,
    DomainStop,
    DomainStopFactory,
)
from kora.plugins.runtime.local.completed import control_file, recorded_exited
from kora.plugins.runtime.local.control import quiesce_command
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.workload_session import Completion
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import EvidenceScope, NormalCompletionQuiescence
from kora.protocols.identity import Handle, ProcessIdentity
from kora.protocols.runtime import Workload
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import DirectoryAnchor, open_local_directory
from kora.runner.identity import (
    GroupObservation,
    IdentityObservation,
    NativeIdentityReader,
)
from kora.runner.peers import kernel_source
from kora.runner.registry import (
    Completed,
    CompletedResult,
    ControlFileRef,
    Running,
    Submitter,
    TransitionExecutor,
)
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    command_workload,
    launched,
)
from tests.plugins.runtime.local.test_command_capture import (
    HeldIO,
    OffsetClock,
    UnresolvedStop,
    log_bytes,
    observational_refusal,
)
from tests.plugins.runtime.local.test_command_observation import (
    SUBMITTER,
    accepted,
    checked_out,
    runtime_answer,
    supervised,
    unless_busy,
)
from tests.plugins.runtime.local.test_command_workspace import settled
from tests.support.local_delivery import DeliveryServer, ReapedSession, delivery_server
from tests.support.local_peers import CONDITION_SECONDS, FAR_NS, identity_of, until
from tests.support.local_runners import written
from tests.support.local_runtime import (
    open_descriptors,
    private_directory,
    protocol_error,
)

__all__ = ["kora_capture"]

SCOPE = (
    EvidenceScope.SUBREAPER_LEADER_GROUP_ENV_SWEEP
    if sys.platform == "linux"
    else EvidenceScope.LEADER_GROUP_ENV_SWEEP
)
SURVIVOR = "\n".join(
    (
        "import os, signal, time",
        "child = os.fork()",
        "if child == 0:",
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        "    time.sleep(60)",
        "    os._exit(0)",
        "open('child.tmp', 'w').write(str(child))",
        "os.rename('child.tmp', 'child.pid')",
    )
)


async def quiesced(
    runtime: LocalRuntime, handle: Handle, ctx: ProtocolContext
) -> NormalCompletionQuiescence:
    return await unless_busy(lambda: runtime.quiescence(handle, ctx))


def completed_entry(runtime: LocalRuntime) -> Completed:
    entry = runtime.registry.entry(runtime.registry.key("attempt-1"))
    assert isinstance(entry, Completed)
    return entry


async def started_legacy(
    runtime: LocalRuntime, ctx: ProtocolContext, script: str = "pass"
) -> Handle:
    handle = await runtime.submit(
        command_workload(runtime, "attempt-1", (sys.executable, "-I", "-c", script)),
        ctx,
    )
    await runtime.start(handle, ctx)
    return handle


async def started_checked_out(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path, script: str
) -> Handle:
    attempt = attempt_directory(root, runtime, "attempt-1")
    handle = await runtime.submit(checked_out(runtime, script), ctx)
    await until(lambda: settled(attempt / "journal.jsonl") == ["prepared"])
    await runtime.start(handle, ctx)
    return handle


async def channel_quiescence(
    channel: BoundedChannel, clock: Clock
) -> NormalCompletionQuiescence | str:
    try:
        await channel.write_typed_frame(
            FrameType.JSON,
            control_request("quiescence", "quiescence-1"),
            clock.monotonic_ns() + FAR_NS,
        )
        _, payload = await channel.read_typed_frame(
            clock.monotonic_ns() + FAR_NS, {FrameType.JSON.value: ENVELOPE_BYTES}
        )
        return read_quiescence(payload, "quiescence-1")
    except KoraProtocolError as error:
        return error.code
    finally:
        channel.close()


async def start_on(channel: BoundedChannel, clock: Clock) -> None:
    await channel.write_typed_frame(
        FrameType.JSON,
        control_request("start", "start-1"),
        clock.monotonic_ns() + FAR_NS,
    )
    _, payload = await channel.read_typed_frame(
        clock.monotonic_ns() + FAR_NS, {FrameType.JSON.value: ENVELOPE_BYTES}
    )
    read_delivery(payload, "start-1", "started")


async def quiesce_as_observer(
    path: str, runner: ProcessIdentity, executor: TransitionExecutor, base: Path
) -> object:
    return await quiesce_command(
        path,
        runner,
        "dispatcher-2",
        kernel_source(NativeIdentityReader()),
        executor,
        SystemClock(),
        "quiescence-1",
    )


class ReusedPidReader:
    def __init__(self) -> None:
        self.native = NativeIdentityReader()
        self.reused = False

    def read(self, pid: int) -> IdentityObservation:
        observation = self.native.read(pid)
        process = observation.process
        if not self.reused or pid != os.getpid() or process is None:
            return observation
        shifted = process.identity.model_copy(
            update={"start_time": process.identity.start_time + 1}
        )
        return replace(observation, process=replace(process, identity=shifted))

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        return self.native.group(process_group_id, deadline_ns)


class OnceUnresolved:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        group: int,
        marker: bytes,
        clock: Clock,
        wake: Callable[[], None],
        prelaunch: dict[int, int] | None,
    ) -> DomainStop:
        self.calls += 1
        if self.calls == 1:
            return UnresolvedStop(group, marker, clock, wake, prelaunch)
        return CommandDomainStop(group, marker, clock, wake, prelaunch)


class HeldSeal(ReapedSession):
    def hold(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def _write_snapshot(self, started: int) -> None:
        self.entered.set()
        self.release.wait(30)
        super()._write_snapshot(started)


def exited_identity() -> ProcessIdentity:
    process = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    process.wait(timeout=30)
    return ProcessIdentity(
        host_id=None,
        boot_id=None,
        pid_namespace=None,
        pid=process.pid,
        start_time=1,
        leads_group=False,
    )


@asynccontextmanager
async def session_for(
    tmp_path: Path,
    capture: Capture,
    session_type: type[ReapedSession] = ReapedSession,
    *,
    clock: Clock | None = None,
    io: HeldIO | None = None,
    stopper: DomainStopFactory = CommandDomainStop,
) -> AsyncGenerator[tuple[ReapedSession, Path]]:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    directory = open_local_directory(attempt)
    session = session_type(
        directory,
        str(attempt / "candidate"),
        SecretScope(capture.diagnostics),
        clock or SystemClock(),
        capture.diagnostics.logger(COMPONENT),
        capture_io=io,
        stopper=stopper,
    )
    if isinstance(session, HeldSeal):
        session.hold()
    try:
        yield session, attempt
    finally:
        if isinstance(session, HeldSeal):
            session.release.set()
        await session.stop()
        directory.close()


async def completion_of(session: ReapedSession) -> list[Completion]:
    seen: list[Completion] = []

    def settled_completion() -> bool:
        current = session.completion()
        if not seen or seen[-1] is not current:
            seen.append(current)
        return current is Completion.COMPLETE

    await until(settled_completion)
    return seen


@pytest.mark.asyncio
async def test_command_attempt_completes_normally(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle = await started_checked_out(runtime, ctx, root, "pass")
        await runtime_answer(runtime, handle, ctx)

        record = await quiesced(runtime, handle, ctx)
        entry = completed_entry(runtime)
        refused = await protocol_error(runtime.start(handle, ctx))
        name = runtime.registry.key("attempt-1").name

    assert record.scope is SCOPE
    assert record.record_ref == f"registry:{name}"
    assert entry.quiescence == record
    assert entry.exit_code == 0
    assert entry.result is not None
    assert entry.result.kind == "command"
    assert refused.code == "attempt_completed"


@pytest.mark.asyncio
async def test_quiescence_requires_the_current_submitter(
    tmp_path: Path, kora_capture: Capture
) -> None:
    reader = ReusedPidReader()
    answers: dict[str, object] = {}
    async with delivery_server(
        private_directory(tmp_path, "server"), kora_capture, identities=reader
    ) as live:
        other = Submitter(dispatcher_id="other", process=live.entry.submitter.process)
        for case, entry in (
            ("superseded", live.entry.model_copy(update={"submitter": other})),
            (
                "superseded_after_gate",
                live.entry.model_copy(update={"submitter": other, "gate_closed": True}),
            ),
            ("current_after_gate", live.entry.model_copy(update={"gate_closed": True})),
        ):
            channel = await live.attach()
            await live.write(entry)
            answers[case] = await channel_quiescence(channel, live.ctx.clock)
            await live.write(live.entry)
        channel = await live.attach()
        reader.reused = True
        answers["reused_pid"] = await channel_quiescence(channel, live.ctx.clock)
        reader.reused = False
    answers["observational"] = await observational_refusal(
        private_directory(tmp_path, "observer"), quiesce_as_observer
    )
    async with launched(private_directory(tmp_path, "gate"), kora_capture) as (
        runtime,
        ctx,
        _root,
    ):
        handle = await started_legacy(runtime, ctx)
        running = runtime.registry.entry(runtime.registry.key("attempt-1"))
        assert isinstance(running, Running)
        await written(
            runtime, "attempt-1", running.model_copy(update={"gate_closed": True}), ctx
        )
        record = await quiesced(runtime, handle, ctx)

    assert answers == {
        "superseded": "submitter_superseded",
        "superseded_after_gate": "submitter_superseded",
        "current_after_gate": "workload_busy",
        "reused_pid": "submitter_superseded",
        "observational": "execution_unauthorized",
    }
    assert record.kind == "normal_completion"


@pytest.mark.asyncio
async def test_quiescence_waits_for_domain_stop(
    tmp_path: Path, kora_capture: Capture
) -> None:
    stopper = OnceUnresolved()
    async with launched(private_directory(tmp_path, "survivor"), kora_capture) as (
        runtime,
        ctx,
        root,
    ):
        handle = await started_legacy(runtime, ctx, SURVIVOR)
        child_file = attempt_directory(root, runtime, "attempt-1") / "candidate"
        await until(lambda: (child_file / "child.pid").exists())
        child = int((child_file / "child.pid").read_text())
        early = await protocol_error(runtime.quiescence(handle, ctx))
        survived = alive(child)
        record = await quiesced(runtime, handle, ctx)
        stopped = not alive(child)
    async with session_for(
        private_directory(tmp_path, "unresolved"), kora_capture, stopper=stopper
    ) as (session, _attempt):
        async with launched(private_directory(tmp_path, "work"), kora_capture) as (
            work_runtime,
            _ctx,
            _root,
        ):
            workload = command_workload(
                work_runtime, "attempt-1", (sys.executable, "-I", "-c", "pass")
            )
        await accepted(session, workload)
        await supervised(session, workload)
        session.attach_drains()
        seen = await completion_of(session)
        reference = session.result_reference()
        retried_observation = session_code(session)

    assert early.code == "workload_busy"
    assert survived
    assert stopped
    assert record.kind == "normal_completion"
    assert seen[0] is not Completion.COMPLETE
    assert Completion.UNRESOLVED in seen
    assert seen[-1] is Completion.COMPLETE
    assert stopper.calls == 2
    assert reference is None
    assert retried_observation == "command_unobserved"


@pytest.mark.asyncio
async def test_quiescence_completes_without_observation(
    tmp_path: Path, kora_capture: Capture
) -> None:
    io = HeldIO(None)
    io.sync_error = OSError("held sync failure")
    async with session_for(
        private_directory(tmp_path, "session"), kora_capture, io=io
    ) as (session, _attempt):
        async with served(
            private_directory(tmp_path, "server"),
            kora_capture,
            session,
            lambda current: checked_out(current, "pass"),
        ) as live:
            record = await unless_busy(lambda live=live: answered(live, live.ctx.clock))
            entry = live.runtime.registry.entry(
                live.runtime.registry.key(live.work.attempt_id)
            )

    assert record.kind == "normal_completion"
    assert isinstance(entry, Completed)
    assert entry.result is None
    assert entry.exit_code == 0


@pytest.mark.asyncio
async def test_runner_exits_after_completion(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(private_directory(tmp_path, "runner"), kora_capture) as (
        runtime,
        ctx,
        root,
    ):
        handle = await started_legacy(runtime, ctx)
        await quiesced(runtime, handle, ctx)
        runner = completed_entry(runtime).runner
        await until(lambda: recorded_exited(NativeIdentityReader(), runner))
        log = (attempt_directory(root, runtime, "attempt-1") / "runner.log").read_text()
    lingering = {
        case: await linger_case(tmp_path, kora_capture, case)
        for case in ("stream_ends", "linger")
    }
    async with session_for(
        private_directory(tmp_path, "seal"), kora_capture, HeldSeal
    ) as (session, _attempt):
        assert isinstance(session, HeldSeal)
        async with served(
            private_directory(tmp_path, "seal-server"),
            kora_capture,
            session,
            lambda current: command_workload(
                current, "attempt-1", (sys.executable, "-I", "-c", "pass")
            ),
        ) as live:
            await until(session.entered.is_set)
            while_sealing = await answered_code(live)
            session.release.set()
            sealed = await unless_busy(lambda live=live: answered(live, live.ctx.clock))
    nameless = exited_identity()
    own = identity_of(os.getpid()).model_copy(
        update={"host_id": None, "boot_id": None, "pid_namespace": None}
    )

    assert "runtime.local.runner.completed" in log
    assert lingering == {
        "stream_ends": (False, True),
        "linger": (False, True),
    }
    assert while_sealing == "workload_busy"
    assert sealed.kind == "normal_completion"
    assert recorded_exited(NativeIdentityReader(), nameless)
    assert not recorded_exited(NativeIdentityReader(), own)
    assert recorded_exited(
        NativeIdentityReader(),
        own.model_copy(update={"start_time": own.start_time + 1}),
    )


async def linger_case(tmp_path: Path, capture: Capture, case: str) -> tuple[bool, bool]:
    clock = OffsetClock()
    async with session_for(
        private_directory(tmp_path, f"{case}-session"), capture, clock=clock
    ) as (session, _attempt):
        async with served(
            private_directory(tmp_path, case),
            capture,
            session,
            lambda current: command_workload(
                current, "attempt-1", (sys.executable, "-I", "-c", "pass")
            ),
            clock=clock,
        ) as live:
            held = await live.attach()
            await completed_while_holding(live, held, clock)
            finished = asyncio.create_task(live.server.completed())
            steps = 5 if case == "stream_ends" else 14
            for _ in range(steps):
                clock.advance(4_000_000_000)
                await status_on(held, clock)
            early = finished.done()
            if case == "stream_ends":
                held.close()
            else:
                clock.advance(4_000_000_000)
                await status_on(held, clock)
            await asyncio.wait_for(asyncio.shield(finished), CONDITION_SECONDS)
            if case == "linger":
                await status_on(held, clock)
            held.close()
            return early, finished.done()


async def completed_while_holding(
    live: DeliveryServer, held: BoundedChannel, clock: Clock
) -> None:
    deadline = time.monotonic() + CONDITION_SECONDS
    while True:
        answer = await channel_quiescence(await live.attach(), clock)
        if not isinstance(answer, str):
            return
        assert answer == "workload_busy"
        assert time.monotonic() < deadline
        await status_on(held, clock)


async def status_on(channel: BoundedChannel, clock: Clock) -> None:
    await channel.write_typed_frame(
        FrameType.JSON,
        control_request("status", "status-1"),
        clock.monotonic_ns() + FAR_NS,
    )
    _, payload = await channel.read_typed_frame(
        clock.monotonic_ns() + FAR_NS, {FrameType.JSON.value: ENVELOPE_BYTES}
    )
    read_status(payload, "status-1")


@asynccontextmanager
async def served(
    tmp_path: Path,
    capture: Capture,
    session: ReapedSession,
    workload: Callable[[LocalRuntime], Workload],
    *,
    clock: Clock | None = None,
) -> AsyncGenerator[DeliveryServer]:
    async with delivery_server(
        tmp_path, capture, clock=clock, command=session, workload=workload
    ) as live:
        channel = await live.attach()
        await live.deliver(channel)
        channel.close()
        await unless_busy(lambda live=live: started_on(live))
        yield live


async def started_on(live: DeliveryServer) -> None:
    channel = await live.attach()
    try:
        await start_on(channel, live.ctx.clock)
    finally:
        channel.close()


async def answered_code(live: DeliveryServer) -> str:
    answer = await channel_quiescence(await live.attach(), live.ctx.clock)
    return answer if isinstance(answer, str) else "completed"


def session_code(session: ReapedSession) -> str:
    try:
        session.command_observation(SUBMITTER)
    except KoraProtocolError as error:
        return error.code
    return "observed"


async def answered(live: DeliveryServer, clock: Clock) -> NormalCompletionQuiescence:
    answer = await channel_quiescence(await live.attach(), clock)
    if isinstance(answer, str):
        raise KoraProtocolError(answer, answer, answer == "workload_busy", {})
    return answer


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_completed_attempt_answers_worker_side(
    tmp_path: Path, kora_capture: Capture
) -> None:
    script = "import os, sys; os.write(1, b'out'); sys.exit(3)"
    async with launched(private_directory(tmp_path, "work"), kora_capture) as (
        runtime,
        ctx,
        root,
    ):
        handle = await started_checked_out(runtime, ctx, root, script)
        observed = await runtime_answer(runtime, handle, ctx)
        record = await quiesced(runtime, handle, ctx)
        entry = completed_entry(runtime)
        await until(lambda: recorded_exited(NativeIdentityReader(), entry.runner))
        attempt = attempt_directory(root, runtime, "attempt-1")

        status = await runtime.status(handle, ctx)
        again = await runtime.quiescence(handle, ctx)
        from_journal = await runtime.command_observation(handle, ctx)
        _, stdout = await log_bytes(runtime, handle, ctx, "stdout", 0)
        token = attempt / "attempt.token"
        token.chmod(0o000)
        no_token = await protocol_error(log_bytes(runtime, handle, ctx, "stdout", 0))
        token.chmod(0o600)
        agent = entry.model_copy(
            update={
                "result": CompletedResult(
                    kind="agent",
                    ref=ControlFileRef(
                        name="result.json", size=1, digest="sha256:" + "0" * 64
                    ),
                )
            }
        )
        await written(runtime, "attempt-1", agent, ctx)
        on_agent = await protocol_error(runtime.command_observation(handle, ctx))
        await written(
            runtime, "attempt-1", entry.model_copy(update={"result": None}), ctx
        )
        on_null = await protocol_error(runtime.command_observation(handle, ctx))
        await written(runtime, "attempt-1", entry, ctx)
        iterators = [runtime.read_log(handle, "stdout", 0, ctx) for _ in range(4)]
        firsts = [await anext(iterator) for iterator in iterators]
        fifth = await protocol_error(anext(runtime.read_log(handle, "stdout", 0, ctx)))
        for iterator in iterators:
            await iterator.aclose()
        (attempt / "stdout.log").write_bytes(b"OUT")
        tampered = await released_frames(runtime, handle, ctx)
        journal = attempt / "journal.jsonl"
        assert entry.result is not None
        offset = entry.result.ref.model_dump()["offset"]
        raw = bytearray(journal.read_bytes())
        raw[offset + 5] ^= 1
        journal.write_bytes(bytes(raw))
        altered = await protocol_error(runtime.command_observation(handle, ctx))
        bad_heads = await refused_heads(runtime, root, entry, ctx, handle)

    assert (status.state, status.phase, status.exit_code) == ("exited", "complete", 3)
    assert again == record
    assert from_journal == observed
    assert stdout == b"out"
    assert no_token.code == "transfer_refused"
    assert firsts == [b"out"] * 4
    assert fifth.code == "workload_busy"
    assert tampered == ([], "integrity_error")
    assert on_agent.code == "command_unobserved"
    assert on_null.code == "command_unobserved"
    assert altered.code == "integrity_error"
    assert bad_heads == ["lineage_unavailable"] * 3


async def released_frames(
    runtime: LocalRuntime, handle: Handle, ctx: ProtocolContext
) -> tuple[list[bytes], str]:
    frames: list[bytes] = []
    try:
        async for frame in runtime.read_log(handle, "stdout", 0, ctx):
            frames.append(frame)
    except KoraProtocolError as error:
        return frames, error.code
    return frames, "read"


async def refused_heads(
    runtime: LocalRuntime,
    root: Path,
    entry: Completed,
    ctx: ProtocolContext,
    handle: Handle,
) -> list[str]:
    key = runtime.registry.key("attempt-1")
    path = root / "registry" / f"{key.name}.json"
    original = path.read_bytes()
    base = json.loads(original)
    codes: list[str] = []
    digest = "sha256:" + "0" * 64
    for result in (
        {"kind": "command", "ref": {"name": "r", "size": 1, "digest": digest}},
        {"kind": "agent", "ref": {"name": "a/b", "size": 1, "digest": digest}},
        {"kind": "agent", "ref": {"name": "r", "size": 16_777_217, "digest": digest}},
    ):
        head = {**base, "entry": {**base["entry"], "result": result}}
        path.write_bytes(json.dumps(head, separators=(",", ":")).encode())
        codes.append((await protocol_error(runtime.status(handle, ctx))).code)
    path.write_bytes(original)
    return codes


def test_control_file_reader_refuses_unsafe_files(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    results = private_directory(private_directory(attempt, "control"), "result")
    payload = b'{"ok":true}'
    (results / "good").write_bytes(payload)
    (results / "good").chmod(0o600)
    (results / "target").write_bytes(payload)
    (results / "target").chmod(0o600)
    (results / "linked").symlink_to(results / "target")
    os.link(results / "target", results / "hard")
    os.mkfifo(results / "fifo", 0o600)
    (results / "large").write_bytes(payload + b"x")
    (results / "large").chmod(0o600)
    anchor = open_local_directory(attempt)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    try:
        good = control_file(
            anchor, ControlFileRef(name="good", size=len(payload), digest=digest)
        )
        refusals = [
            refusal_code(
                anchor, ControlFileRef(name=name, size=len(payload), digest=digest)
            )
            for name in ("linked", "hard", "fifo", "large")
        ]
    finally:
        anchor.close()

    assert good == payload
    assert refusals == [
        "transfer_refused",
        "transfer_refused",
        "transfer_refused",
        "integrity_error",
    ]


def refusal_code(anchor: DirectoryAnchor, ref: ControlFileRef) -> str:
    try:
        control_file(anchor, ref)
    except KoraProtocolError as error:
        return error.code
    return "read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["success", "refusal", "cancellation", "shutdown", "parked_read"]
)
async def test_completion_frees_resources(
    tmp_path: Path, kora_capture: Capture, case: str
) -> None:
    descriptors = open_descriptors()
    pending: list[asyncio.Task[NormalCompletionQuiescence]] = []
    parked: list[AsyncGenerator[bytes]] = []
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        if case == "refusal":
            handle = await runtime.submit(
                command_workload(
                    runtime, "attempt-1", (sys.executable, "-I", "-c", "pass")
                ),
                ctx,
            )
            outcome = (await protocol_error(runtime.quiescence(handle, ctx))).code
        elif case == "parked_read":
            script = "import os; os.write(1, b'out')"
            handle = await started_checked_out(runtime, ctx, root, script)
            await runtime_answer(runtime, handle, ctx)
            await quiesced(runtime, handle, ctx)
            runner = completed_entry(runtime).runner
            await until(lambda: recorded_exited(NativeIdentityReader(), runner))
            reader = runtime.read_log(handle, "stdout", 0, ctx)
            parked.append(reader)
            outcome = (await anext(reader)).decode()
        else:
            handle = await started_legacy(runtime, ctx)
            if case == "success":
                await quiesced(runtime, handle, ctx)
                runner = completed_entry(runtime).runner
                await until(lambda: recorded_exited(NativeIdentityReader(), runner))
                outcome = "completed"
            else:
                pending.append(asyncio.create_task(runtime.quiescence(handle, ctx)))
                if case == "cancellation":
                    pending[0].cancel()
                outcome = case
    settled = await asyncio.gather(*pending, return_exceptions=True)
    await until(lambda: open_descriptors() <= descriptors)

    expected = {
        "success": "completed",
        "refusal": "workload_busy",
        "cancellation": "cancellation",
        "shutdown": "shutdown",
        "parked_read": "out",
    }
    assert outcome == expected[case]
    assert all(
        isinstance(item, NormalCompletionQuiescence | BaseException) for item in settled
    )
    assert open_descriptors() <= descriptors
