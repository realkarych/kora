import asyncio
import fcntl
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
from kora.observability import LoggingSettings
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    PRE_ATTACH_BYTES,
    AttachRequest,
    DispatcherQuiescenceRequest,
    FrameType,
    read_delivery,
    read_dispatcher_quiescence,
)
from kora.plugins.runtime.local.completed import recorded_exited
from kora.plugins.runtime.local.control import attached_request, attested, connected
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.plugins.runtime.local.spawn import runner_command
from kora.plugins.runtime.local.supervisor import EXIT_FAILED
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import DispatcherClosure, DispatcherQuiescence
from kora.protocols.history import (
    GRANT_HISTORY_LABEL,
    GrantHistoryPage,
    GrantHistoryPosition,
    GrantHistorySnapshot,
    chain_seed,
    chain_step,
    record_bytes,
)
from kora.protocols.identity import MAX_COUNT, MAX_PID, GrantBinding, ProcessIdentity
from kora.runner.channels import BoundedChannel, ChannelError
from kora.runner.identity import NativeIdentityReader
from kora.runner.peers import kernel_source
from kora.runner.registry import (
    FIRST_GRANT_ARGUMENT,
    HEAD_LIMIT,
    PREFIX_BYTES,
    RETAINED_LIMIT,
    AttemptKey,
    Completed,
    FirstClosureRecord,
    FirstGrantHead,
    FirstGrantLinks,
    FirstGrantRecord,
    RetainedReference,
    Running,
    Unsubmitted,
    model_bytes,
    runner_argv,
)
from tests.plugins.runtime.local.frames_support import StepClock
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    command_workload,
    fill,
    release_commands,
)
from tests.plugins.runtime.local.test_command_observation import unless_busy
from tests.support.local_peers import (
    CONDITION_SECONDS,
    FAR_NS,
    POLL_SECONDS,
    identity_of,
    until,
)
from tests.support.local_runners import (
    reap_runners,
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
    runtime: LocalRuntime, ctx: ProtocolContext
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
        GRANT_HISTORY_LABEL, ctx.namespace_id, ATTEMPT, runtime.execution_domain_id
    )
    snapshot = GrantHistorySnapshot(
        version=1,
        namespace_id=ctx.namespace_id,
        attempt_id=ATTEMPT,
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
        attempt_id=ATTEMPT,
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


def lock_is_free(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


async def discard_reply(
    runtime: LocalRuntime,
    running: Running,
    request: DispatcherQuiescenceRequest,
    ctx: ProtocolContext,
    closure_path: Path,
) -> None:
    source = kernel_source(NativeIdentityReader())
    cutoff = time.monotonic() + CONDITION_SECONDS
    while not closure_path.exists():
        assert time.monotonic() < cutoff
        deadline = ctx.clock.monotonic_ns() + FAR_NS
        endpoint = await connected(running.socket_path, ctx.clock, deadline)
        channel = BoundedChannel(endpoint, ctx.clock)
        try:
            await attested(endpoint, running.runner, source, runtime.executor)
            attach = AttachRequest(
                op="attach",
                request_id=request.request_id,
                dispatcher_id=running.submitter.dispatcher_id,
            )
            await channel.write_typed_frame(
                FrameType.JSON, model_bytes(attach), deadline
            )
            _, payload = await channel.read_typed_frame(
                deadline, {FrameType.JSON.value: ENVELOPE_BYTES}
            )
            read_delivery(payload, request.request_id, "attached")
            await channel.write_typed_frame(
                FrameType.JSON, model_bytes(request), deadline
            )
        finally:
            channel.close()
        await asyncio.sleep(POLL_SECONDS)
    key = runtime.registry.key(ATTEMPT)
    await until(lambda: current_closure(runtime, key) is not None)


def current_closure(runtime: LocalRuntime, key: AttemptKey) -> RetainedReference | None:
    head = runtime.registry.read(key)
    return head.first_grant.closure if isinstance(head, FirstGrantHead) else None


async def cancel_while_waiting_for_lock(
    runtime: LocalRuntime,
    ctx: ProtocolContext,
    grant: GrantBinding,
    ack: DispatcherQuiescence,
    lock_path: Path,
) -> None:
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        clock = StepClock()
        pending = asyncio.create_task(
            runtime.record_dispatcher_quiescence(
                ATTEMPT, ack, grant, replace(ctx, clock=clock)
            )
        )
        try:
            await asyncio.wait_for(clock.waiting.wait(), CONDITION_SECONDS)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert not runtime.executor.jobs
    finally:
        os.close(descriptor)


async def journal_full(runtime: LocalRuntime, ctx: ProtocolContext, root: Path) -> None:
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
    key = runtime.registry.key(ATTEMPT)
    head = runtime.registry.read(key)
    attempt = attempt_directory(root, runtime, ATTEMPT)
    fill(attempt)
    try:
        await expect_refusal("journal_unavailable", runtime.start(handle, ctx))
        await expect_refusal(
            "journal_unavailable",
            runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx),
        )

        assert runtime.registry.read(key) == head
        assert not (root / "registry" / f"{key.name}.first-closure.v1.json").exists()
    finally:
        (attempt / "filler").unlink()


async def exercise(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path, *, discard: bool
) -> None:
    grant, page, ack = binding(runtime, ctx)
    key = runtime.registry.key(ATTEMPT)
    head_path = root / "registry" / f"{key.name}.json"
    lock_path = head_path.with_suffix(".lock")
    grant_path = head_path.with_suffix(".first-grant.v1.json")
    closure_path = head_path.with_suffix(".first-closure.v1.json")
    attempt = attempt_directory(root, runtime, ATTEMPT)
    workload = command_workload(
        runtime,
        ATTEMPT,
        (sys.executable, "-I", "-c", "pass"),
        dispatcher_id=grant.dispatcher_id,
    )

    await expect_refusal("registry_unavailable", runtime.workspace_target(ATTEMPT, ctx))
    await expect_refusal("registry_unavailable", runtime.submit(workload, ctx))
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
    handle = await runtime.submit(workload, ctx)
    running = runtime.registry.entry(key)
    assert isinstance(running, Running)

    await expect_refusal(
        "workload_busy", runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
    )
    await expect_refusal("workload_busy", runtime.quiescence(handle, ctx))

    assert not closure_path.exists()
    assert isinstance(runtime.registry.entry(key), Running)

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
            request,
            ENVELOPE_BYTES,
        )
    assert not closure_path.exists()
    if discard:
        await discard_reply(runtime, running, request, ctx, closure_path)
    else:
        await unless_busy(
            lambda: runtime.record_dispatcher_quiescence(ATTEMPT, ack, grant, ctx)
        )
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

    assert stamps(head_path, grant_path, closure_path) == attached
    assert isinstance(runtime.registry.entry(key), Running)
    head = runtime.registry.read(key)
    assert isinstance(head, FirstGrantHead)
    leaf = runtime.registry.linked_closure(key, head, grant)
    assert leaf is not None
    assert leaf.closure.quiescence == ack
    assert leaf.closure.provenance == "registry_acknowledgement"

    record = await unless_busy(lambda: runtime.quiescence(handle, ctx))
    await until(lambda: recorded_exited(NativeIdentityReader(), running.runner))
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

    await cancel_while_waiting_for_lock(runtime, ctx, grant, ack, lock_path)
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
    ]
    for argv in wrong:
        result = await supervised(argv)
        assert result.code == EXIT_FAILED
        assert result.readiness == b""
        assert not (root / "attempts").exists()
    without_marker = await supervised((*selected[:4], *selected[5:]))

    assert without_marker.code == EXIT_FAILED
    assert without_marker.readiness == b""
    assert stamps(head) == before
    assert runtime.registry.entry(key) == entry
    assert len(list(attempt.iterdir())) == 2


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
                await journal_full(runtime, ctx, root)
            else:
                await exercise(runtime, ctx, root, discard=case == "lost")
        finally:
            await runtime.close()
        events = [
            event
            for event in capture.finish()
            if event.get("event") == "runtime.local.first_grant"
        ]
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
        if case in {"normal", "lost"}:
            assert ("closure", "confirmed", True) in outcomes
        if case != "launch":
            assert ("closure", "refused", True) in outcomes


def run_worker(root: Path, case: str) -> subprocess.CompletedProcess[str]:
    with short_directory() as base:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.plugins.runtime.local.test_first_grant",
                    str(root),
                    str(base / "sockets"),
                    case,
                ],
                start_new_session=True,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        finally:
            release_commands(root)
            reap_runners(root)

    return result


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


def test_first_local_grant_runner_refuses_mismatched_startup(tmp_path: Path) -> None:
    root = state_root(tmp_path)

    result = run_worker(root, "launch")

    assert result.returncode == 0, result.stderr


@pytest.mark.host_fixture
def test_first_local_grant_refuses_a_closed_journal_on_full_disk() -> None:
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


if __name__ == "__main__":
    asyncio.run(original_worker(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
