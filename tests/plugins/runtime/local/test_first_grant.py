import asyncio
import errno
import fcntl
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
from kora.observability import LoggingSettings
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    FRAME_PROGRESS_NS,
    PRE_ATTACH_BYTES,
    AttachRequest,
    DispatcherQuiescenceRequest,
    FrameType,
    control_request,
    read_delivery,
    read_dispatcher_quiescence,
)
from kora.plugins.runtime.local.completed import recorded_exited
from kora.plugins.runtime.local.control import attached_request, attested, connected
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.plugins.runtime.local.spawn import CompletionPipe, runner_command
from kora.plugins.runtime.local.supervisor import EXIT_FAILED
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import (
    DispatcherClosure,
    DispatcherQuiescence,
    EvidenceScope,
)
from kora.protocols.history import (
    GRANT_HISTORY_LABEL,
    GrantHistoryPage,
    GrantHistoryPosition,
    GrantHistorySnapshot,
    chain_seed,
    chain_step,
    record_bytes,
)
from kora.protocols.identity import (
    MAX_COUNT,
    MAX_PID,
    GrantBinding,
    Handle,
    ProcessIdentity,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel, ChannelError
from kora.runner.identity import ArgumentMatch, IdentityState, NativeIdentityReader
from kora.runner.peers import PeerRefused, kernel_source, live_identity
from kora.runner.processes import ExitAnchor, SignalResult
from kora.runner.registry import (
    ADMITTED_JOBS,
    FIRST_GRANT_ARGUMENT,
    HEAD_LIMIT,
    PREFIX_BYTES,
    RETAINED_LIMIT,
    SOCKET_NAME_LENGTH,
    AttemptKey,
    Completed,
    FirstClosureRecord,
    FirstGrantHead,
    FirstGrantLinks,
    FirstGrantRecord,
    RetainedReference,
    Retired,
    Running,
    Starting,
    TransitionExecutor,
    Unsubmitted,
    model_bytes,
    runner_argv,
    socket_path,
)
from tests.plugins.runtime.local.frames_support import StepClock
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    command_workload,
    fill,
    release_commands,
)
from tests.plugins.runtime.local.test_command_observation import unless_busy
from tests.plugins.runtime.local.test_transfer import live_descriptors
from tests.support.local_peers import (
    CONDITION_SECONDS,
    identity_of,
    until,
)
from tests.support.local_runners import (
    PS,
    SHORT_BASE,
    reap_runners,
    runner_pids,
    short_directory,
    starting_entry,
    supervised,
    written,
)
from tests.support.local_runtime import (
    open_descriptors,
    prepared,
    runtime_context,
    state_root,
)

ATTEMPT = "attempt-1"


def binding(
    runtime: LocalRuntime, ctx: ProtocolContext, attempt_id: str = ATTEMPT
) -> tuple[GrantBinding, GrantHistoryPage, DispatcherQuiescence]:
    grant = GrantBinding(
        dispatcher_id="original-dispatcher",
        worker_id="original-worker",
        incarnation_id="original-incarnation",
        lease_epoch=1,
        execution_domain_id=runtime.execution_domain_id,
        process=identity_of(os.getpid()),
    )
    seed = chain_seed(
        GRANT_HISTORY_LABEL, ctx.namespace_id, attempt_id, runtime.execution_domain_id
    )
    snapshot = GrantHistorySnapshot(
        version=1,
        namespace_id=ctx.namespace_id,
        attempt_id=attempt_id,
        execution_domain_id=runtime.execution_domain_id,
        upper_epoch=grant.lease_epoch,
        count=1,
        digest=chain_step(seed, record_bytes(grant)),
    )
    page = GrantHistoryPage(
        snapshot=snapshot,
        start=GrantHistoryPosition(
            after_epoch=None, received_count=0, prefix_digest=seed
        ),
        grants=(grant,),
        end=snapshot.tip(),
        complete=True,
    )
    ack = DispatcherQuiescence(
        attempt_id=attempt_id,
        execution_domain_id=runtime.execution_domain_id,
        dispatcher_id=grant.dispatcher_id,
        worker_incarnation_id=grant.incarnation_id,
        lease_generation=grant.lease_epoch,
        closed_at=ctx.clock.now(),
        evidence_ref="original-closed",
    )
    return grant, page, ack


async def expect_refusal(code: str, call: Awaitable[object]) -> None:
    with pytest.raises(KoraProtocolError) as refused:
        await call
    assert refused.value.code == code


def stamps(*paths: Path) -> tuple[tuple[bytes, int, int, int], ...]:
    output: list[tuple[bytes, int, int, int]] = []
    for path in paths:
        stat = path.stat()
        output.append(
            (path.read_bytes(), stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
        )
    return tuple(output)


def unclosed_grant(
    runtime: LocalRuntime, key: AttemptKey, head_path: Path
) -> tuple[FirstGrantLinks, bytes]:
    head = runtime.registry.read(key)
    assert isinstance(head, FirstGrantHead)
    assert head.first_grant.closure is None
    assert not head_path.with_suffix(".first-closure.v1.json").exists()
    return head.first_grant, head_path.with_suffix(".first-grant.v1.json").read_bytes()


def lock_is_free(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


async def discard_confirmed_reply(
    runtime: LocalRuntime,
    running: Running,
    request: DispatcherQuiescenceRequest,
    ctx: ProtocolContext,
) -> None:
    payload = await attached_request(
        running.socket_path,
        running.runner,
        running.submitter.dispatcher_id,
        kernel_source(NativeIdentityReader()),
        runtime.executor,
        ctx.clock,
        request.request_id,
        model_bytes(request),
        ENVELOPE_BYTES,
    )
    read_delivery(payload, request.request_id, "dispatcher_quiescence_recorded")


async def disconnect_before_completion_reply(
    runtime: LocalRuntime, running: Running, ctx: ProtocolContext
) -> None:
    request_id = "lost-normal-completion"
    deadline = ctx.clock.monotonic_ns() + FRAME_PROGRESS_NS
    endpoint = await connected(running.socket_path, ctx.clock, deadline)
    channel = BoundedChannel(endpoint, ctx.clock)
    key = runtime.registry.key(ATTEMPT)
    try:
        await attested(
            endpoint,
            running.runner,
            kernel_source(NativeIdentityReader()),
            runtime.executor,
        )
        request = AttachRequest(
            op="attach",
            request_id=request_id,
            dispatcher_id=running.submitter.dispatcher_id,
        )
        await channel.write_typed_frame(FrameType.JSON, model_bytes(request), deadline)
        _, attached = await channel.read_typed_frame(
            deadline, {FrameType.JSON.value: ENVELOPE_BYTES}
        )
        read_delivery(attached, request_id, "attached")
        await channel.write_typed_frame(
            FrameType.JSON, control_request("quiescence", request_id), deadline
        )
        await until(lambda: isinstance(runtime.registry.entry(key), Completed))
    finally:
        channel.close()


class ReadyCancellationClock(SystemClock):
    def __init__(self, runtime: LocalRuntime, cancel: Callable[[], None]) -> None:
        self.runtime = runtime
        self.cancel = cancel
        self.observed_ready = False

    def monotonic_ns(self) -> int:
        if not self.observed_ready and not self.runtime.launchers.spawning:
            entry = self.runtime.registry.entry(self.runtime.registry.key(ATTEMPT))
            if isinstance(entry, Running):
                self.observed_ready = True
                self.cancel()
        return super().monotonic_ns()


def fifo_identities() -> set[tuple[int, int]]:
    pipes: set[tuple[int, int]] = set()
    for descriptor in live_descriptors():
        try:
            identity = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            if stat.S_ISFIFO(identity.st_mode):
                pipes.add((identity.st_dev, identity.st_ino))
    return pipes


def pipe_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    assert stat.S_ISFIFO(metadata.st_mode)
    return metadata.st_dev, metadata.st_ino


def pipe_pending(descriptor: int, identity: tuple[int, int]) -> bool:
    assert pipe_identity(descriptor) == identity
    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    return not poller.poll(0)


class StartupTimeoutClock(SystemClock):
    def __init__(self, runtime: LocalRuntime, key: AttemptKey, lock: int) -> None:
        self.runtime = runtime
        self.key = key
        self.lock = lock
        self.held = False
        self.cutoff_ns: int | None = None
        self.reader: int | None = None
        self.reader_identity: tuple[int, int] | None = None
        self.observed_ns: int | None = None
        self.baseline = fifo_identities()

    def monotonic_ns(self) -> int:
        if not self.held and isinstance(
            self.runtime.registry.entry(self.key), Starting
        ):
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
        return super().monotonic_ns()

    async def sleep_until(self, deadline_ns: int) -> None:
        observe_readiness = (
            self.held and self.runtime.launchers.spawning and self.cutoff_ns is None
        )
        if not observe_readiness:
            await super().sleep_until(deadline_ns)
            return
        readers: list[int] = []
        for descriptor in live_descriptors():
            metadata = os.fstat(descriptor)
            if (
                stat.S_ISFIFO(metadata.st_mode)
                and (
                    metadata.st_dev,
                    metadata.st_ino,
                )
                not in self.baseline
            ):
                readers.append(descriptor)
        assert len(readers) == 1
        descriptor = readers[0]
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        assert flags & os.O_ACCMODE == os.O_RDONLY
        assert flags & os.O_NONBLOCK
        assert not os.get_inheritable(descriptor)
        self.reader = descriptor
        identity = pipe_identity(descriptor)
        self.reader_identity = identity
        self.cutoff_ns = deadline_ns
        await super().sleep_until(deadline_ns)
        assert pipe_pending(descriptor, identity)
        self.observed_ns = super().monotonic_ns()


def supervisor_stopped(original: ProcessIdentity) -> bool:
    state = subprocess.run(
        [PS, "-p", str(original.pid), "-o", "state="],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    return state.stdout.strip().startswith("T")


async def startup_supervisor(path: Path, cutoff_ns: int) -> ProcessIdentity:
    reader = NativeIdentityReader()
    endpoint = await connected(str(path), SystemClock(), cutoff_ns)
    try:
        original = kernel_source(reader).attest(endpoint, None)
        assert original.pid > 1
        assert original.pid not in {os.getpid(), os.getppid()}
        return original
    finally:
        endpoint.close()


def signal_startup_supervisor(
    anchor: ExitAnchor,
    original: ProcessIdentity,
    expected: tuple[str, ...],
    requested: signal.Signals,
) -> None:
    reader = NativeIdentityReader()
    assert reader.carries(original.pid, expected) is ArgumentMatch.EQUAL
    assert live_identity(reader, original.pid) == original
    assert anchor.signal(requested) is SignalResult.SENT


def resume_startup_supervisor(
    anchor: ExitAnchor, original: ProcessIdentity, expected: tuple[str, ...]
) -> bool:
    if anchor.exited:
        return True
    if sys.platform == "darwin":
        reader = NativeIdentityReader()
        try:
            if reader.carries(original.pid, expected) is not ArgumentMatch.EQUAL:
                return False
            if live_identity(reader, original.pid) != original:
                return False
        except (OSError, PeerRefused):
            return False
    return anchor.signal(signal.SIGCONT) is SignalResult.SENT


async def hold_startup_writer(
    runtime: LocalRuntime,
    clock: StartupTimeoutClock,
    root: Path,
    path: Path,
    pending: asyncio.Task[None],
) -> None:
    await until(
        lambda: pending.done() or (clock.cutoff_ns is not None and path.is_socket())
    )
    starting = runtime.registry.entry(clock.key)
    assert clock.held
    assert isinstance(starting, Starting)
    assert not pending.done()
    descriptor = clock.reader
    identity = clock.reader_identity
    cutoff = clock.cutoff_ns
    assert descriptor is not None
    assert identity is not None
    assert cutoff is not None
    original = await startup_supervisor(path, cutoff)
    expected = runner_argv(starting.runner_prefix, clock.key, starting.nonce)
    owner = ExitAnchor(original.pid, direct_child=False)
    try:
        assert not owner.unknown
        assert not owner.exited
        sys.stdout.write(
            json.dumps(
                {
                    "case": "startup-owner",
                    "pid": original.pid,
                    "start": original.start_time,
                    "socket": str(path),
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        signal_startup_supervisor(owner, original, expected, signal.SIGSTOP)
        try:
            await until(lambda: pending.done() or supervisor_stopped(original))
            assert runner_pids(root) == (original.pid,)
            stopped_ns = clock.monotonic_ns()
            assert stopped_ns < cutoff
            assert not pending.done()
            assert pipe_pending(descriptor, identity)

            await pending

            assert clock.observed_ns is not None
            assert clock.observed_ns >= cutoff
            assert supervisor_stopped(original)
            assert not owner.exited
            assert not owner.unknown
            assert runtime.registry.entry(clock.key) == starting
            assert fifo_identities() == clock.baseline
            with pytest.raises(OSError, match=os.strerror(errno.EBADF)) as closed:
                os.fstat(descriptor)
            assert closed.value.errno == errno.EBADF
            assert not runtime.launchers.spawning
            assert not runtime.launchers.settling
            assert not runtime.launchers.unsettled
            assert runner_pids(root) == (original.pid,)
        finally:
            await until(lambda: resume_startup_supervisor(owner, original, expected))
            await until(lambda: owner.reaped or owner.unknown)
            assert owner.exited
            assert owner.reaped
            assert not owner.unknown
        assert NativeIdentityReader().read(original.pid).state is IdentityState.ABSENT
        assert runtime.registry.entry(clock.key) == starting
        assert not path.exists()
        assert not runner_pids(root)
        sys.stdout.write(
            json.dumps(
                {
                    "case": "startup-timeout",
                    "supervisor_pid": original.pid,
                    "supervisor_start": original.start_time,
                    "stopped_ns": stopped_ns,
                    "cutoff_ns": cutoff,
                    "writer_pending_at_cutoff_ns": clock.observed_ns,
                    "native_exit": owner.exited,
                    "native_reap": owner.reaped,
                    "reader_identity": identity,
                }
            )
            + "\n"
        )
    finally:
        owner.close()


async def startup_timeout(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path, sockets: Path
) -> None:
    key = runtime.registry.key(ATTEMPT)
    path = Path(socket_path(str(sockets), key.name[:SOCKET_NAME_LENGTH]))
    deadline = ExecutionDeadline(
        at=ctx.clock.now() + timedelta(seconds=8),
        scope_kind="attempt",
        scope_id=ATTEMPT,
        clock_uncertainty=timedelta(0),
        grace=DEADLINE_GRACE,
    )
    grant, page, _ = binding(runtime, ctx)
    await runtime.accept_grant_history(
        ATTEMPT, grant, page, replace(ctx, deadline=deadline)
    )
    workload = command_workload(
        runtime,
        ATTEMPT,
        (sys.executable, "-I", "-c", "pass"),
        dispatcher_id=grant.dispatcher_id,
        deadline=TypeAdapter[ExecutionDeadline](ExecutionDeadline).dump_python(
            deadline, mode="json"
        ),
    )
    lock = os.open(root / "registry" / f"{key.name}.lock", os.O_RDWR)
    clock = StartupTimeoutClock(runtime, key, lock)
    pending = asyncio.create_task(
        expect_refusal(
            "runner_starting",
            runtime.submit(workload, replace(ctx, clock=clock, deadline=deadline)),
        )
    )
    try:
        await hold_startup_writer(runtime, clock, root, path, pending)
        attempt = attempt_directory(root, runtime, ATTEMPT)
        records = [
            json.loads(line)
            for line in (attempt / "runner.log").read_bytes().splitlines()
        ]
        refused = [
            record
            for record in records
            if record.get("event") == "runtime.local.runner.refused"
        ]
        assert [record.get("reason") for record in refused] == ["lock"]
        assert not (attempt / "candidate").exists()
    finally:
        try:
            await asyncio.gather(pending, return_exceptions=True)
        finally:
            os.close(lock)


async def cancel_admitted_replays(
    runtime: LocalRuntime,
    ctx: ProtocolContext,
    grant: GrantBinding,
    ack: DispatcherQuiescence,
    lock_path: Path,
) -> None:
    descriptor = os.open(lock_path, os.O_RDWR)
    clock = StepClock()
    blocked = replace(ctx, clock=clock)

    def retry() -> Awaitable[None]:
        return runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, blocked)

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = [
            asyncio.create_task(
                runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, blocked)
            )
            for _ in range(ADMITTED_JOBS)
        ]
        try:
            await until(lambda: len(clock.sleepers) == ADMITTED_JOBS)
            await expect_refusal("registry_busy", retry())
        finally:
            for task in pending:
                task.cancel()
            outcomes = await asyncio.gather(*pending, return_exceptions=True)

        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert not runtime.executor.jobs
    finally:
        os.close(descriptor)


def settled_command(log: Path) -> bool:
    raw = log.read_bytes()
    records = [json.loads(line) for line in raw[: raw.rfind(b"\n") + 1].splitlines()]
    return sum(
        record.get("event") == "runtime.local.capture" for record in records
    ) == 2 and any(
        record.get("event") == "runtime.local.seal"
        and record.get("outcome") == "sealed"
        for record in records
    )


async def closure_storage_full(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path
) -> None:
    grant, page, ack = binding(runtime, ctx)
    await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    handle = await runtime.submit(
        command_workload(
            runtime,
            ATTEMPT,
            (sys.executable, "-I", "-c", "pass"),
            dispatcher_id=grant.dispatcher_id,
        ),
        ctx,
    )
    await runtime.start(handle, ctx)
    key = runtime.registry.key(ATTEMPT)
    head = runtime.registry.read(key)
    attempt = attempt_directory(root, runtime, ATTEMPT)
    await until(lambda: settled_command(attempt / "runner.log"))
    fill(attempt)

    try:
        await expect_refusal(
            "journal_unavailable",
            unless_busy(
                lambda: runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
            ),
        )
    finally:
        (attempt / "filler").unlink()
    await expect_refusal("journal_unavailable", runtime.quiescence(handle, ctx))
    await expect_refusal(
        "journal_unavailable",
        runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
    )

    assert runtime.registry.read(key) == head
    assert not (root / "registry" / f"{key.name}.first-closure.v1.json").exists()


async def release_with_contender(
    runtime: LocalRuntime,
    handle: Handle,
    ctx: ProtocolContext,
    attempt: Path,
    evidence_paths: tuple[Path, ...],
) -> None:
    releases = runtime.releases
    available_before = releases.available
    holding = threading.Event()
    allow_cleanup = threading.Event()
    allow_contender = threading.Event()
    key = runtime.registry.key(ATTEMPT)
    original = runtime.registry.read(key)
    assert isinstance(original, FirstGrantHead)
    assert isinstance(original.entry, Completed)
    assert original.entry.released_at is None
    before = stamps(*evidence_paths)

    def hold(gate: threading.Event) -> None:
        holding.set()
        assert gate.wait(CONDITION_SECONDS)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(releases.run(lambda: hold(allow_cleanup)))
        try:
            await until(holding.is_set)
            first = tasks.create_task(runtime.release(handle, ctx))
            await until(lambda: len(releases.waiting) == 1)
            tasks.create_task(releases.run(lambda: hold(allow_contender)))
            await until(lambda: len(releases.waiting) == 2)
            contender = tasks.create_task(runtime.release(handle, ctx))
            await until(lambda: len(releases.waiting) == 3)

            assert not first.done()
            assert not contender.done()
            assert attempt.is_dir()
            assert stamps(*evidence_paths) == before

            allow_cleanup.set()
            await first

            assert not contender.done()
            assert len(releases.waiting) == 1
            assert not attempt.exists()
            released = runtime.registry.read(key)
            assert isinstance(released, FirstGrantHead)
            assert isinstance(released.entry, Completed)
            assert released.entry.released_at is not None
            expected = original.entry.model_copy(
                update={"released_at": released.entry.released_at}
            )
            assert released == original.model_copy(update={"entry": expected})
            after = stamps(*evidence_paths)
            assert after[1:] == before[1:]

            allow_contender.set()
            await contender
        finally:
            allow_cleanup.set()
            allow_contender.set()

    assert stamps(*evidence_paths) == after
    assert not releases.waiting
    assert not releases.jobs
    assert releases.available == available_before


async def exercise(
    runtime: LocalRuntime,
    ctx: ProtocolContext,
    root: Path,
    sockets: Path,
    *,
    discard: bool,
    discard_completion: bool = False,
) -> None:
    grant, page, ack = binding(runtime, ctx)
    key = runtime.registry.key(ATTEMPT)
    head_path = root / "registry" / f"{key.name}.json"
    lock_path = head_path.with_suffix(".lock")
    grant_path = head_path.with_suffix(".first-grant.v1.json")
    closure_path = head_path.with_suffix(".first-closure.v1.json")
    attempt = attempt_directory(root, runtime, ATTEMPT)
    hold_command = not discard and not discard_completion
    gate_path = root / "command-gate"
    waiting_marker = b"waiting for gate\n"
    command = "pass"
    if hold_command:
        command = "\n".join(
            (
                "import fcntl, os",
                f"os.write(1, {waiting_marker!r})",
                f"with open({str(gate_path)!r}, 'rb') as gate:",
                "    fcntl.flock(gate, fcntl.LOCK_SH)",
            )
        )
    elif discard_completion:
        command = "\n".join(
            (
                "import errno, os, stat, sys",
                "for name in os.listdir('/dev/fd'):",
                "    try:",
                "        value = os.fstat(int(name))",
                "    except OSError as error:",
                "        if error.errno != errno.EBADF:",
                "            raise",
                "    else:",
                "        if int(name) > 2 and stat.S_ISFIFO(value.st_mode):",
                "            sys.exit(43)",
            )
        )
    workload = command_workload(
        runtime,
        ATTEMPT,
        (sys.executable, "-I", "-c", command),
        dispatcher_id=grant.dispatcher_id,
    )
    sockets.rmdir()

    await expect_refusal("registry_unavailable", runtime.workspace_target(ATTEMPT, ctx))
    await expect_refusal("registry_unavailable", runtime.submit(workload, ctx))

    assert not sockets.exists()
    empty = await runtime.grant_history_status(ATTEMPT, grant, page.snapshot, ctx)

    assert empty.accepted == page.start
    assert not empty.complete
    assert not lock_path.exists()
    assert not head_path.exists()
    assert not attempt.exists()

    foreign_process = identity_of(os.getppid())
    wrong_origin = grant.model_copy(update={"process": foreign_process})

    await expect_refusal(
        "lineage_unavailable",
        runtime.accept_grant_history(ATTEMPT, wrong_origin, page, ctx),
    )
    saved = await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    head = runtime.registry.read(key)

    assert saved.complete
    assert saved.accepted == page.end
    assert isinstance(head, FirstGrantHead)
    assert isinstance(head.entry, Unsubmitted)
    assert head.first_grant.closure is None
    assert grant_path.exists()
    assert not closure_path.exists()
    assert not attempt.exists()

    await runtime.workspace_target(ATTEMPT, ctx)
    before_pipes = fifo_identities()
    reader_identity: tuple[int, int] | None = None
    cancelled_running: Running | None = None
    if discard_completion:
        retained_grant = (head.first_grant, grant_path.read_bytes())
        pending: asyncio.Task[Handle] | None = None

        def cancel_submit() -> None:
            assert pending is not None
            pending.cancel()

        clock = ReadyCancellationClock(runtime, cancel_submit)
        pending = asyncio.create_task(
            runtime.submit(workload, replace(ctx, clock=clock))
        )
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert clock.observed_ready
        ready = runtime.registry.entry(key)
        assert isinstance(ready, Running)
        cancelled_running = ready
        assert unclosed_grant(runtime, key, head_path) == retained_grant
        new_pipes = fifo_identities() - before_pipes
        assert len(new_pipes) == 1
        reader_identity = new_pipes.pop()

    handle = await runtime.submit(workload, ctx)
    running = runtime.registry.entry(key)
    assert isinstance(running, Running)
    if cancelled_running is not None:
        assert running == cancelled_running
        assert fifo_identities() - before_pipes == {reader_identity}
    if hold_command:
        running_evidence = stamps(head_path, grant_path, lock_path)

        running_intake = await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)

        assert running_intake == saved
        assert stamps(head_path, grant_path, lock_path) == running_evidence

    await expect_refusal(
        "workload_busy", runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
    )
    await expect_refusal("workload_busy", runtime.quiescence(handle, ctx))

    assert not closure_path.exists()
    assert isinstance(runtime.registry.entry(key), Running)

    if hold_command:
        output = attempt / "stdout.log"
        with gate_path.open("wb") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await runtime.start(handle, ctx)
            await until(
                lambda: output.exists() and output.read_bytes() == waiting_marker
            )
            held = stamps(head_path, grant_path)

            await expect_refusal(
                "workload_busy",
                runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
            )
            await expect_refusal("workload_busy", runtime.quiescence(handle, ctx))

            assert stamps(head_path, grant_path) == held
            assert not closure_path.exists()
        await until(lambda: settled_command(attempt / "runner.log"))
    else:
        await runtime.start(handle, ctx)
    request = DispatcherQuiescenceRequest(
        op="record_dispatcher_quiescence",
        request_id="first-closure",
        grant=grant,
        acknowledgement=ack,
    )
    with pytest.raises(ChannelError):
        await attached_request(
            running.socket_path,
            running.runner,
            "foreign-dispatcher",
            kernel_source(NativeIdentityReader()),
            runtime.executor,
            ctx.clock,
            request.request_id,
            model_bytes(request),
            ENVELOPE_BYTES,
        )
    assert not closure_path.exists()
    pre_closure_head = head_path.read_bytes()
    if hold_command:
        leaf = FirstClosureRecord(
            format="local-first-closure/1",
            closure=DispatcherClosure(
                quiescence=ack, provenance="registry_acknowledgement", process=None
            ),
        )
        with closure_path.open("xb") as occupied:
            occupied.write(model_bytes(leaf))
        closure_path.chmod(0o600)
        before_collision = stamps(head_path, grant_path, closure_path, lock_path)

        await expect_refusal(
            "workload_conflict",
            unless_busy(
                lambda: runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
            ),
        )

        assert (
            stamps(head_path, grant_path, closure_path, lock_path) == before_collision
        )
        closure_path.unlink()

        first_head = FirstGrantHead.model_validate_json(pre_closure_head)
        changed_entry = running.model_copy(
            update={"workload_digest": "sha256:" + "a" * 64}
        )
        assert changed_entry.workload_digest != running.workload_digest
        changed_head = first_head.model_copy(update={"entry": changed_entry})
        fixture_head = head_path.with_suffix(".fixture-head")
        fixture_head.write_bytes(model_bytes(changed_head))
        fixture_head.chmod(0o600)
        fixture_head.replace(head_path)
        before_retry = stamps(head_path, grant_path, lock_path)
        try:
            await expect_refusal(
                "lineage_unavailable",
                runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
            )

            assert stamps(head_path, grant_path, lock_path) == before_retry
            assert not closure_path.exists()
        finally:
            fixture_head.write_bytes(pre_closure_head)
            fixture_head.chmod(0o600)
            fixture_head.replace(head_path)
    if discard:
        await unless_busy(
            lambda: discard_confirmed_reply(runtime, running, request, ctx)
        )
    else:
        await unless_busy(
            lambda: runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
        )
    if hold_command:
        confirmed_head = head_path.read_bytes()
        fixture_head = head_path.with_suffix(".fixture-head")
        fixture_closure = closure_path.with_suffix(".fixture-closure")
        fixture_head.write_bytes(pre_closure_head)
        fixture_head.chmod(0o600)
        fixture_head.replace(head_path)
        closure_path.rename(fixture_closure)
        missing_evidence = stamps(head_path, grant_path, lock_path, fixture_closure)
        try:
            await expect_refusal(
                "lineage_unavailable",
                runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
            )

            assert (
                stamps(head_path, grant_path, lock_path, fixture_closure)
                == missing_evidence
            )
            assert not closure_path.exists()
        finally:
            fixture_closure.replace(closure_path)
            fixture_head.write_bytes(confirmed_head)
            fixture_head.chmod(0o600)
            fixture_head.replace(head_path)
    attached = stamps(head_path, grant_path, closure_path)
    await runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
    lock_is_free(lock_path)
    changed = ack.model_copy(update={"evidence_ref": "other-confirmation"})
    await expect_refusal(
        "workload_conflict",
        runtime.record_dispatcher_quiescence(ATTEMPT, changed, grant, ctx),
    )

    await expect_refusal("workload_conflict", runtime.start(handle, ctx))
    await expect_refusal("workload_conflict", runtime.submit(workload, ctx))
    if hold_command:
        await expect_refusal(
            "retirement_unavailable", runtime.retire(ATTEMPT, timedelta(0), ctx)
        )
    spool_before = tuple(sorted((root / "spools").rglob("*")))
    await expect_refusal(
        "workload_conflict", anext(runtime.transfer_out(handle, "result", ctx))
    )

    assert tuple(sorted((root / "spools").rglob("*"))) == spool_before
    assert stamps(head_path, grant_path, closure_path) == attached
    assert isinstance(runtime.registry.entry(key), Running)
    head = runtime.registry.read(key)
    assert isinstance(head, FirstGrantHead)
    leaf = runtime.registry.linked_closure(key, head, grant)
    assert leaf is not None
    assert leaf.closure.quiescence == ack
    assert leaf.closure.provenance == "registry_acknowledgement"

    if discard_completion:
        competing = "other-attempt"
        other_grant, other_history, _ = binding(runtime, ctx, competing)
        await runtime.accept_grant_history(competing, other_grant, other_history, ctx)
        other = runtime.registry.key(competing)
        other_head = runtime.registry.read(other)
        other_attempt = attempt_directory(root, runtime, competing)
        other_workload = command_workload(
            runtime,
            competing,
            (sys.executable, "-I", "-c", "pass"),
            dispatcher_id=grant.dispatcher_id,
        )

        await expect_refusal("runner_starting", runtime.submit(other_workload, ctx))

        assert runtime.registry.read(other) == other_head
        assert isinstance(runtime.registry.entry(other), Unsubmitted)
        assert not other_attempt.exists()

        await disconnect_before_completion_reply(runtime, running, ctx)
        await until(lambda: recorded_exited(NativeIdentityReader(), running.runner))
        await expect_refusal("lineage_unavailable", runtime.release(handle, ctx))
        assert attempt.exists()
        original = runtime.registry.read(key)
        assert isinstance(original, FirstGrantHead)
        assert isinstance(original.entry, Completed)
        completed_bytes = head_path.read_bytes()
        altered_entry = original.entry.model_copy(
            update={"gate_closed": not original.entry.gate_closed}
        )
        altered_head = original.model_copy(update={"entry": altered_entry})
        head_path.write_bytes(model_bytes(altered_head))
        try:
            await expect_refusal("lineage_unavailable", runtime.quiescence(handle, ctx))
            await expect_refusal("lineage_unavailable", runtime.release(handle, ctx))

            assert head_path.read_bytes() == model_bytes(altered_head)
            assert attempt.exists()
        finally:
            head_path.write_bytes(completed_bytes)

        record = await runtime.quiescence(handle, ctx)

        assert record == original.entry.quiescence
        assert original.entry.exit_code == 0
        assert reader_identity is not None
        assert reader_identity not in fifo_identities()
        assert head_path.read_bytes() == completed_bytes
    else:
        record = await unless_busy(lambda: runtime.quiescence(handle, ctx))
        await until(lambda: recorded_exited(NativeIdentityReader(), running.runner))

    if hold_command:
        completed_head = runtime.registry.read(key)
        assert isinstance(completed_head, FirstGrantHead)
        assert isinstance(completed_head.entry, Completed)
        links = FirstGrantLinks(grant=completed_head.first_grant.grant, closure=None)
        head_path.write_bytes(
            model_bytes(completed_head.model_copy(update={"first_grant": links}))
        )
        inconsistent = stamps(head_path, grant_path, closure_path, lock_path)
        try:
            await expect_refusal(
                "lineage_unavailable", runtime.workspace_target(ATTEMPT, ctx)
            )
            await expect_refusal("lineage_unavailable", runtime.submit(workload, ctx))
            await expect_refusal(
                "lineage_unavailable",
                runtime.accept_grant_history(ATTEMPT, grant, page, ctx),
            )
            await expect_refusal("lineage_unavailable", runtime.release(handle, ctx))

            assert (
                stamps(head_path, grant_path, closure_path, lock_path) == inconsistent
            )
            assert attempt.exists()
        finally:
            head_path.write_bytes(model_bytes(completed_head))

    completed_evidence = stamps(head_path, grant_path, closure_path)

    await expect_refusal("workload_conflict", runtime.workspace_target(ATTEMPT, ctx))
    await expect_refusal("workload_conflict", runtime.submit(workload, ctx))

    assert stamps(head_path, grant_path, closure_path) == completed_evidence

    if hold_command:
        await release_with_contender(
            runtime,
            handle,
            ctx,
            attempt,
            (head_path, grant_path, closure_path, lock_path),
        )
    else:
        await runtime.release(handle, ctx)
    completed = runtime.registry.entry(key)

    assert isinstance(completed, Completed)
    assert completed.released_at is not None
    assert completed.quiescence == record
    assert not attempt.exists()
    assert not Path(running.socket_path).exists()

    detached = stamps(head_path, grant_path, closure_path, lock_path)
    sentinel = head_path.with_suffix(".json.tmp-local2")
    sentinel.touch(mode=0o600)
    descriptors = open_descriptors()

    await expect_refusal("attempt_retired", runtime.workspace_target(ATTEMPT, ctx))
    await expect_refusal("attempt_retired", runtime.submit(workload, ctx))
    await cancel_admitted_replays(runtime, ctx, grant, ack, lock_path)
    await runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
    await expect_refusal(
        "workload_conflict",
        runtime.record_dispatcher_quiescence(ATTEMPT, changed, grant, ctx),
    )
    await expect_refusal(
        "workload_conflict",
        runtime.record_dispatcher_quiescence(ATTEMPT, ack, wrong_origin, ctx),
    )

    assert stamps(head_path, grant_path, closure_path, lock_path) == detached
    assert sentinel.exists()
    assert open_descriptors() == descriptors
    lock_is_free(lock_path)

    canonical_head = head_path.read_bytes()
    head_path.write_bytes(b'{"format":"local/2",' + canonical_head[1:])
    await expect_refusal(
        "lineage_unavailable",
        runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
    )
    head_path.write_bytes(canonical_head)

    data = closure_path.read_bytes()
    closure_path.unlink()
    await expect_refusal(
        "lineage_unavailable",
        runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
    )
    assert not closure_path.exists()
    closure_path.write_bytes(data)
    closure_path.chmod(0o600)
    lock_path.unlink()

    await expect_refusal(
        "registry_unavailable",
        runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
    )

    assert not lock_path.exists()


async def refuse_missing_intake(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path
) -> None:
    grant, page, _ = binding(runtime, ctx)
    key = runtime.registry.key(ATTEMPT)
    head = root / "registry" / f"{key.name}.json"
    lock = head.with_suffix(".lock")
    leaf = head.with_suffix(".first-grant.v1.json")
    accepted = await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    original = stamps(head, lock, leaf)

    repeated = await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)

    assert accepted.complete
    assert repeated == accepted
    assert stamps(head, lock, leaf) == original

    head.unlink()
    lock.unlink()
    retained = stamps(leaf)

    await expect_refusal(
        "lineage_unavailable", runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    )

    assert not head.exists()
    assert not lock.exists()
    assert stamps(leaf) == retained

    leaf.unlink()
    before = tuple((root / "registry").iterdir())

    await expect_refusal(
        "lineage_unavailable", runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    )

    assert tuple((root / "registry").iterdir()) == before
    assert not head.exists()
    assert not lock.exists()
    assert not leaf.exists()


async def verify_intake_lineage(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path
) -> None:
    grant, page, _ = binding(runtime, ctx)
    key = runtime.registry.key(ATTEMPT)
    head = root / "registry" / f"{key.name}.json"
    lock = head.with_suffix(".lock")
    leaf = head.with_suffix(".first-grant.v1.json")
    record = model_bytes(
        FirstGrantRecord(
            format="local-first-grant/1", snapshot=page.snapshot, grant=grant
        )
    )
    leaf.write_bytes(record)
    leaf.chmod(0o600)

    await expect_refusal(
        "workload_conflict", runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    )

    assert not head.exists()
    assert lock.exists()
    assert leaf.read_bytes() == record

    initial = FirstGrantHead(
        format="local/2",
        execution_domain_id=key.execution_domain_id,
        attempt_id=key.attempt_id,
        entry=Unsubmitted(
            state="unsubmitted", deadline=ctx.deadline, gate_closed=False
        ),
        first_grant=FirstGrantLinks(
            grant=RetainedReference.describe(record), closure=None
        ),
    )
    deadline = ExecutionDeadline(
        at=ctx.clock.now() + timedelta(minutes=1),
        scope_kind="attempt",
        scope_id=ATTEMPT,
        clock_uncertainty=timedelta(0),
        grace=DEADLINE_GRACE,
    )
    wrong_entry = Unsubmitted(state="unsubmitted", deadline=deadline, gate_closed=False)
    wrong_head = initial.model_copy(update={"entry": wrong_entry})
    head.write_bytes(model_bytes(wrong_head))
    head.chmod(0o600)
    wrong_evidence = stamps(head, leaf, lock)

    await expect_refusal(
        "lineage_unavailable", runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    )

    assert stamps(head, leaf, lock) == wrong_evidence

    head.write_bytes(model_bytes(initial))
    exact_evidence = stamps(head, leaf, lock)

    accepted = await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)

    assert accepted.complete
    assert accepted.accepted == page.end
    assert stamps(head, leaf, lock) == exact_evidence

    head.write_bytes(model_bytes(wrong_head))
    wrong_evidence = stamps(head, leaf, lock)

    await expect_refusal(
        "lineage_unavailable", runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    )

    assert stamps(head, leaf, lock) == wrong_evidence


async def wrong_launch(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path, sockets: Path
) -> None:
    grant, page, _ = binding(runtime, ctx)
    await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    nonce = "a" * 32
    key = runtime.registry.key(ATTEMPT)
    entry = starting_entry(
        sockets,
        nonce,
        dispatcher=grant.dispatcher_id,
        prefix=(*runner_command(), FIRST_GRANT_ARGUMENT, str(root)),
    )
    await written(runtime, ATTEMPT, entry, ctx)
    selected = runner_argv(entry.runner_prefix, key, nonce)
    head = root / "registry" / f"{key.name}.json"
    attempt = attempt_directory(root, runtime, ATTEMPT)
    before = stamps(head)

    wrong = [
        (*selected[:-2], "foreign-domain", nonce),
        (*selected[:-1], "b" * 32),
        (*selected[:4], "--closure-profile=local/3", *selected[5:]),
        (*selected[:4], *selected[5:]),
    ]
    for argv in wrong:
        result = await supervised(argv)
        assert result.code == EXIT_FAILED
        assert result.readiness == b""
        assert not (root / "attempts").exists()
    assert stamps(head) == before
    assert runtime.registry.entry(key) == entry
    assert not attempt.exists()


async def leave_running(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path
) -> None:
    grant, page, ack = binding(runtime, ctx)
    await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    await runtime.submit(
        command_workload(
            runtime,
            ATTEMPT,
            (sys.executable, "-I", "-c", "pass"),
            dispatcher_id=grant.dispatcher_id,
        ),
        ctx,
    )
    running = runtime.registry.entry(runtime.registry.key(ATTEMPT))
    assert isinstance(running, Running)
    request = DispatcherQuiescenceRequest(
        op="record_dispatcher_quiescence",
        request_id="observer",
        grant=grant,
        acknowledgement=ack,
    )
    (root / "observer-request").write_bytes(model_bytes(request))
    (root / "observer-running").write_bytes(model_bytes(running))


def sockets_open() -> set[int]:
    sockets: set[int] = set()
    for descriptor in live_descriptors():
        try:
            mode = os.fstat(descriptor).st_mode
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            if stat.S_ISSOCK(mode):
                sockets.add(descriptor)
    return sockets


async def close_during_attach(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path, *, completion: bool = False
) -> None:
    grant, page, ack = binding(runtime, ctx)
    await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    before_launch = fifo_identities()
    handle = await runtime.submit(
        command_workload(
            runtime,
            ATTEMPT,
            (sys.executable, "-I", "-c", "pass"),
            dispatcher_id=grant.dispatcher_id,
        ),
        ctx,
    )
    key = runtime.registry.key(ATTEMPT)
    running = runtime.registry.entry(key)
    assert isinstance(running, Running)
    identities = NativeIdentityReader()
    assert identity_of(running.runner.pid) == running.runner
    if completion:
        await expect_refusal(
            "workload_busy",
            runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
        )
        await runtime.start(handle, ctx)
        await unless_busy(
            lambda: runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
        )
    before = sockets_open()
    readers = fifo_identities() - before_launch
    assert len(readers) == 1
    head = root / "registry" / f"{key.name}.json"
    before_head = stamps(head)

    os.kill(running.runner.pid, signal.SIGSTOP)
    try:
        pending: list[asyncio.Task[object]] = []
        clock = StepClock()
        step = replace(ctx, clock=clock)
        lock = None
        try:
            if completion:
                other = "held-closure-attempt"
                other_grant, page, other_ack = binding(runtime, ctx, other)
                await runtime.accept_grant_history(other, other_grant, page, ctx)
                other_key = runtime.registry.key(other)
                lock = os.open(root / "registry" / f"{other_key.name}.lock", os.O_RDWR)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                pending.extend(
                    asyncio.create_task(
                        runtime.record_dispatcher_quiescence(
                            other, other_ack, other_grant, step
                        )
                    )
                    for _ in range(ADMITTED_JOBS - 1)
                )
                await until(lambda: len(clock.sleepers) == ADMITTED_JOBS - 1)
            pending.append(
                asyncio.create_task(
                    runtime.quiescence(handle, step)
                    if completion
                    else runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, step)
                )
            )
            await until(lambda: bool(sockets_open() - before))
            attached = sockets_open() - before
            if completion:
                await expect_refusal("registry_busy", runtime.quiescence(handle, step))
                assert sockets_open() - before == attached

            await runtime.close()

            assert sockets_open().isdisjoint(attached)
            assert readers.isdisjoint(fifo_identities())
            assert runtime.executor.joined
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
            assert all(
                isinstance(outcome, asyncio.CancelledError) for outcome in outcomes
            )
            assert (
                root / "registry" / f"{key.name}.first-closure.v1.json"
            ).exists() == completion
            assert stamps(head) == before_head
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if lock is not None:
                os.close(lock)
    finally:
        if not recorded_exited(identities, running.runner):
            os.kill(running.runner.pid, signal.SIGCONT)


async def refused_startup(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path
) -> None:
    installation = root / "runner.json"
    installation.write_bytes(b"[]")
    installation.chmod(0o600)
    grant, page, _ = binding(runtime, ctx)
    await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    workload = command_workload(
        runtime,
        ATTEMPT,
        (sys.executable, "-I", "-c", "pass"),
        dispatcher_id=grant.dispatcher_id,
    )
    key = runtime.registry.key(ATTEMPT)
    head_path = root / "registry" / f"{key.name}.json"
    retained_grant = unclosed_grant(runtime, key, head_path)
    readers = fifo_identities()

    await expect_refusal("runner_starting", runtime.submit(workload, ctx))

    assert fifo_identities() == readers
    starting = runtime.registry.entry(key)
    assert isinstance(starting, Starting)
    assert starting.fence is None
    assert not runtime.launchers.unsettled
    assert not runner_pids(root)
    assert unclosed_grant(runtime, key, head_path) == retained_grant

    receipt = await runtime.retire(ATTEMPT, timedelta(0), ctx)
    retired = runtime.registry.entry(key)

    assert isinstance(retired, Retired)
    assert retired.receipt == receipt
    assert receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert not runner_pids(root)
    assert unclosed_grant(runtime, key, head_path) == retained_grant


async def expire_unsubmitted(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path
) -> None:
    clock = StepClock()
    deadline = ExecutionDeadline(
        at=clock.now() + timedelta(seconds=2),
        scope_kind="attempt",
        scope_id=ATTEMPT,
        clock_uncertainty=timedelta(seconds=1),
        grace=DEADLINE_GRACE,
    )
    ctx = replace(ctx, clock=clock, deadline=deadline)
    grant, page, _ = binding(runtime, ctx)
    key = runtime.registry.key(ATTEMPT)
    head_path = root / "registry" / f"{key.name}.json"

    accepted = await runtime.accept_grant_history(ATTEMPT, grant, page, ctx)
    entry = runtime.registry.entry(key)

    assert accepted.complete
    assert accepted.accepted == page.end
    assert isinstance(entry, Unsubmitted)
    assert entry.deadline == deadline
    assert not entry.gate_closed
    retained_grant = unclosed_grant(runtime, key, head_path)

    clock.advance(999_999_000)
    await runtime.workspace_target(ATTEMPT, ctx)
    clock.advance(1_000)
    await expect_refusal("deadline_expired", runtime.workspace_target(ATTEMPT, ctx))
    expired = runtime.registry.entry(key)

    assert isinstance(expired, Unsubmitted)
    assert expired.deadline == deadline
    assert expired.gate_closed
    assert unclosed_grant(runtime, key, head_path) == retained_grant

    after_expiry = stamps(head_path)
    clock.offset_ns = 0
    await expect_refusal("deadline_expired", runtime.workspace_target(ATTEMPT, ctx))

    assert stamps(head_path) == after_expiry

    receipt = await runtime.retire(ATTEMPT, timedelta(0), ctx)
    retired = runtime.registry.entry(key)

    assert isinstance(retired, Retired)
    assert retired.receipt == receipt
    assert receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert not attempt_directory(root, runtime, ATTEMPT).exists()
    assert not runner_pids(root)
    assert unclosed_grant(runtime, key, head_path) == retained_grant


async def original_worker(root: Path, sockets: Path, case: str) -> None:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "first-grant-test", utc_now
    ) as capture:
        ctx = runtime_context(capture)
        runtime = await LocalRuntime.open(
            LocalRuntimeConfiguration(state_root=str(root)),
            ctx,
            socket_directory=sockets,
            first_grant=True,
        )
        try:
            if case == "launch":
                await wrong_launch(runtime, ctx, root, sockets)
            elif case == "full":
                await closure_storage_full(runtime, ctx, root)
            elif case == "startup-refusal":
                await refused_startup(runtime, ctx, root)
            elif case == "startup-timeout":
                await startup_timeout(runtime, ctx, root, sockets)
            elif case == "missing-intake":
                await refuse_missing_intake(runtime, ctx, root)
            elif case == "intake-lineage":
                await verify_intake_lineage(runtime, ctx, root)
            elif case == "expiry":
                await expire_unsubmitted(runtime, ctx, root)
            elif case == "observer":
                await leave_running(runtime, ctx, root)
            elif case in {"shutdown", "shutdown-completion"}:
                await close_during_attach(
                    runtime, ctx, root, completion=case == "shutdown-completion"
                )
            else:
                await exercise(
                    runtime,
                    ctx,
                    root,
                    sockets,
                    discard=case == "lost",
                    discard_completion=case == "completion-lost",
                )
        finally:
            await runtime.close()
        observed = capture.finish()
        if case == "normal":
            release_counts = [
                event.get("removed")
                for event in observed
                if event.get("event") == "runtime.local.release"
                and event.get("outcome") == "released"
            ]
            assert len(release_counts) == 2
            first_removed, contender_removed = release_counts
            assert isinstance(first_removed, int)
            assert first_removed > 0
            assert contender_removed == 0
            assert runtime.releases.joined
        events = [
            event
            for event in observed
            if event.get("event") == "runtime.local.first_grant"
        ]
        if case in {"startup-refusal", "startup-timeout"}:
            expected = "ended" if case == "startup-refusal" else "timeout"
            assert [
                event["outcome"]
                for event in observed
                if event.get("event") == "runtime.local.spawn"
            ] == [expected]
        for value in (
            str(root),
            "original-worker",
            "original-incarnation",
            "original-closed",
        ):
            assert value not in str(events)
        outcomes = [
            (
                event.get("operation"),
                event.get("outcome"),
                isinstance(event.get("waited_ms"), (int, float)),
            )
            for event in events
        ]
        assert ("intake", "confirmed", True) in outcomes
        if case in {"normal", "lost", "completion-lost"}:
            assert ("closure", "confirmed", True) in outcomes
        if case not in {
            "expiry",
            "intake-lineage",
            "launch",
            "missing-intake",
            "observer",
            "startup-refusal",
            "startup-timeout",
        }:
            assert ("closure", "refused", True) in outcomes


def start_worker(
    root: Path, sockets: Path, case: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.plugins.runtime.local.test_first_grant",
            str(root),
            str(sockets),
            case,
        ],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def run_worker(root: Path, case: str) -> subprocess.CompletedProcess[str]:
    with short_directory() as base:
        try:
            return start_worker(root, base / "sockets", case)
        finally:
            release_commands(root)
            reap_runners(root)


@pytest.mark.asyncio
async def test_first_local_grant_runner_refuses_native_observer(tmp_path: Path) -> None:
    root = state_root(tmp_path)
    identities = NativeIdentityReader()
    executor = TransitionExecutor()
    with short_directory() as base:
        try:
            result = await asyncio.to_thread(
                start_worker, root, base / "sockets", "observer"
            )
            assert result.returncode == 0, result.stderr
            running = Running.model_validate_json(
                (root / "observer-running").read_bytes()
            )
            request = DispatcherQuiescenceRequest.model_validate_json(
                (root / "observer-request").read_bytes()
            )
            assert recorded_exited(identities, running.submitter.process)
            assert not recorded_exited(identities, running.runner)
            key = AttemptKey(request.grant.execution_domain_id, ATTEMPT)
            head = root / "registry" / f"{key.name}.json"
            before = stamps(head)

            payload = await attached_request(
                running.socket_path,
                running.runner,
                running.submitter.dispatcher_id,
                kernel_source(identities),
                executor,
                SystemClock(),
                request.request_id,
                model_bytes(request),
                ENVELOPE_BYTES,
            )

            with pytest.raises(KoraProtocolError) as refused:
                read_delivery(
                    payload, request.request_id, "dispatcher_quiescence_recorded"
                )
            assert refused.value.code == "execution_unauthorized"
            assert stamps(head) == before
            assert not head.with_suffix(".first-closure.v1.json").exists()
        finally:
            await executor.close()
            release_commands(root)
            reap_runners(root)


def test_first_local_grant_closure_replays_across_normal_release(
    tmp_path: Path,
) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "normal")

    assert result.returncode == 0, result.stderr


def test_first_local_grant_closure_replays_after_lost_reply(tmp_path: Path) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "lost")

    assert result.returncode == 0, result.stderr


def test_first_local_grant_does_not_recreate_missing_confirmed_intake(
    tmp_path: Path,
) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "missing-intake")

    assert result.returncode == 0, result.stderr


def test_first_local_grant_retry_requires_exact_intake_lineage(tmp_path: Path) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "intake-lineage")

    assert result.returncode == 0, result.stderr


def test_first_local_grant_releases_after_lost_normal_completion(
    tmp_path: Path,
) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "completion-lost")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(b"", b""), (b"C", b""), (b"C" * 34, b""), (b"C" * 33, b"C" * 33)],
)
def test_first_local_grant_completion_pipe_requires_exact_frame_and_eof(
    payload: bytes, expected: bytes
) -> None:
    reader, writer = os.pipe()
    pipe = CompletionPipe(AttemptKey("local-domain", ATTEMPT), "f" * 32, reader=reader)
    try:
        os.set_blocking(reader, False)
        try:
            os.write(writer, payload)
        finally:
            os.close(writer)

        first = pipe.collect()
        held = pipe.collect()

        assert first == held == expected
        with pytest.raises(OSError, match=os.strerror(errno.EBADF)) as closed:
            os.fstat(reader)
        assert closed.value.errno == errno.EBADF
    finally:
        pipe.close()


def test_first_local_grant_completion_pipe_requires_writer_close() -> None:
    reader, writer = os.pipe()
    pipe = CompletionPipe(AttemptKey("local-domain", ATTEMPT), "f" * 32, reader=reader)
    try:
        os.set_blocking(reader, False)
        os.write(writer, b"C" * 33)

        proof = pipe.collect()

        assert proof == b""
        assert pipe.reader is None
    finally:
        os.close(writer)
        pipe.close()


def test_first_local_grant_runner_refuses_mismatched_startup(tmp_path: Path) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "launch")

    assert result.returncode == 0, result.stderr


def test_first_local_grant_startup_refusal_reaches_eof_and_closes_reader(
    tmp_path: Path,
) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "startup-refusal")

    assert result.returncode == 0, result.stderr


def test_first_local_grant_expiry_and_retirement_preserve_original_grant(
    tmp_path: Path,
) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "expiry")

    assert result.returncode == 0, result.stderr


def test_first_local_grant_startup_timeout_holds_original_writer() -> None:
    base = Path(tempfile.mkdtemp(prefix="k1773-", dir=SHORT_BASE)).resolve()
    root = state_root(base)
    sockets = base / "sockets"

    result = start_worker(root, sockets, "startup-timeout")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not runner_pids(root)
    shutil.rmtree(base)
    sys.stdout.write(result.stdout)


@pytest.mark.parametrize("case", ["shutdown", "shutdown-completion"])
def test_first_local_grant_runtime_close_joins_attached_socket(
    tmp_path: Path, case: str
) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, case)

    assert result.returncode == 0, result.stderr


@pytest.mark.host_fixture
def test_first_local_grant_storage_full_fences_normal_completion() -> None:
    with tempfile.TemporaryDirectory(dir=prepared("small-filesystem")) as directory:
        root = state_root(Path(directory))

        result = run_worker(root, "full")

        assert result.returncode == 0, result.stderr


def test_first_local_grant_codec_holds_legally_maximal_records() -> None:
    longest = "\\" * 256
    at = datetime.max.replace(tzinfo=UTC)
    process = ProcessIdentity(
        host_id=longest,
        boot_id=longest,
        pid_namespace=longest,
        pid=MAX_PID,
        start_time=MAX_COUNT,
        leads_group=True,
    )
    grant = GrantBinding(
        dispatcher_id=longest,
        worker_id=longest,
        incarnation_id=longest,
        lease_epoch=MAX_COUNT,
        execution_domain_id=longest,
        process=process,
    )
    seed = chain_seed(GRANT_HISTORY_LABEL, longest, longest, longest)
    snapshot = GrantHistorySnapshot(
        version=1,
        namespace_id=longest,
        execution_domain_id=longest,
        attempt_id=longest,
        upper_epoch=MAX_COUNT,
        count=1,
        digest=chain_step(seed, record_bytes(grant)),
    )
    ack = DispatcherQuiescence(
        attempt_id=longest,
        execution_domain_id=longest,
        dispatcher_id=longest,
        worker_incarnation_id=longest,
        lease_generation=MAX_COUNT,
        closed_at=at,
        evidence_ref=longest,
    )
    frame = DispatcherQuiescenceRequest(
        op="record_dispatcher_quiescence",
        request_id=longest,
        grant=grant,
        acknowledgement=ack,
    )
    grant_record = FirstGrantRecord(
        format="local-first-grant/1", snapshot=snapshot, grant=grant
    )
    closure = FirstClosureRecord(
        format="local-first-closure/1",
        closure=DispatcherClosure(
            provenance="registry_acknowledgement",
            process=None,
            quiescence=ack,
        ),
    )
    runner_prefix = ("\\" * (PREFIX_BYTES - 3), "/")
    start = starting_entry(
        Path("/sockets"), "a" * 32, dispatcher=longest, prefix=runner_prefix
    )
    entry = type(start).model_validate(
        {
            **start.model_dump(),
            "submitter": {
                "dispatcher_id": longest,
                "process": process,
            },
        }
    )

    frame_bytes = model_bytes(frame)
    grant_bytes, closure_bytes = model_bytes(grant_record), model_bytes(closure)
    links = FirstGrantLinks(
        grant=RetainedReference.describe(grant_bytes),
        closure=RetainedReference.describe(closure_bytes),
    )
    head = FirstGrantHead(
        format="local/2",
        execution_domain_id=longest,
        attempt_id=longest,
        entry=entry,
        first_grant=links,
    )
    head_bytes = model_bytes(head)

    assert len(frame_bytes) <= PRE_ATTACH_BYTES
    decoded = read_dispatcher_quiescence(frame_bytes)
    assert decoded is not None
    assert model_bytes(decoded) == frame_bytes
    assert (
        len(grant_bytes) + len(closure_bytes) + len(model_bytes(links)) < RETAINED_LIMIT
    )
    assert len(head_bytes) <= HEAD_LIMIT
    assert model_bytes(FirstGrantHead.model_validate_json(head_bytes)) == head_bytes

    expanded = type(entry).model_validate(
        {**entry.model_dump(), "runner_prefix": ("\x01" * (PREFIX_BYTES - 3), "/")}
    )

    with pytest.raises(ValidationError, match="the registry head exceeds its bound"):
        FirstGrantHead.model_validate({**head.model_dump(), "entry": expanded})


if __name__ == "__main__":
    asyncio.run(original_worker(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
