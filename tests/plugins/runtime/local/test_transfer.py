import asyncio
import errno
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import LoggingSettings, configure_process
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    FRAME_PROGRESS_NS,
    MESSAGE_PROGRESS_NS,
    SECTION_FRAME_BYTES,
    AttachRequest,
    FrameType,
    TransferRequest,
    read_delivery,
)
from kora.plugins.runtime.local.command import INCOMING, CommandSession
from kora.plugins.runtime.local.control import attested, connected, transfer_input
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import COMPONENT, ControlServer, rebound
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Handle
from kora.protocols.runtime import TransferItem
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel, ChannelError
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.journal import read_records
from kora.runner.native_io import NativeIOSubmissionUncertain, NativeIOUnresolved
from kora.runner.peers import kernel_source
from kora.runner.registry import Registry, Running, TransitionExecutor, takeover_cell
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    binary,
    command_workload,
    descriptor_numbers,
    fill,
    launched,
    reap_runners,
    release_commands,
    running_pid,
)
from tests.support.local_delivery import delivery_server
from tests.support.local_peers import identity_of, until
from tests.support.local_runners import (
    exited_within,
    open_spawning,
    runner_pids,
    short_directory,
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


def digest_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def input_item(
    name: str,
    payload: bytes,
    *,
    kind: Literal["resource", "input", "script"] = "input",
    sha256: str | None = None,
) -> TransferItem:
    return TransferItem(
        name=name,
        size=len(payload),
        sha256=digest_of(payload) if sha256 is None else sha256,
        kind=kind,
    )


async def chunks(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def descriptor_identities() -> dict[int, tuple[int, int]]:
    descriptors: dict[int, tuple[int, int]] = {}
    for descriptor in descriptor_numbers(os.getpid()):
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            descriptors[descriptor] = (metadata.st_dev, metadata.st_ino)
    return descriptors


def live_descriptors() -> set[int]:
    return set(descriptor_identities())


def journal_kinds(attempt: Path) -> list[str]:
    anchor = open_local_directory(attempt)
    try:
        return [kind for _, kind in read_records(anchor)]
    finally:
        anchor.close()


def journal_fields(attempt: Path) -> tuple[str, str]:
    for line in (attempt / "journal.jsonl").read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("kind") != "transfer.materialised":
            continue
        data = record["data"]
        assert set(data) == {"name", "sha256"}
        name = data["name"]
        digest = data["sha256"]
        assert isinstance(name, str)
        assert isinstance(digest, str)
        return name, digest
    pytest.fail("the materialised record is missing", pytrace=False)


def transfer_diagnostics(path: Path) -> list[tuple[str, int]]:
    hidden = {"name", "sha256", "path", "pid", "token", "argv", "attempt_id"}
    found: list[tuple[str, int]] = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != "runtime.local.transfer":
            continue
        outcome = record.get("outcome")
        waited = record.get("waited_ms")
        assert isinstance(outcome, str)
        assert isinstance(waited, int)
        assert hidden.isdisjoint(record)
        found.append((outcome, waited))
    return found


def stored_text(attempt: Path) -> str:
    journal = attempt / "journal.jsonl"
    if not journal.exists():
        return ""
    return journal.read_text()


async def accepted(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path, attempt: str
) -> tuple[Handle, Path]:
    workload = command_workload(runtime, attempt, (binary("true"),))
    handle = await runtime.submit(workload, ctx)
    return handle, attempt_directory(root, runtime, attempt)


@asynccontextmanager
async def transfer_channel(
    runtime: LocalRuntime, ctx: ProtocolContext, entry: Running
) -> AsyncGenerator[BoundedChannel]:
    deadline = ctx.clock.monotonic_ns() + FRAME_PROGRESS_NS
    endpoint = await connected(entry.socket_path, ctx.clock, deadline)
    channel = BoundedChannel(endpoint, ctx.clock)
    try:
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        await attested(
            endpoint,
            entry.runner,
            kernel_source(NativeIdentityReader()),
            runtime.executor,
        )
        request = AttachRequest(
            op="attach", request_id="wire", dispatcher_id=entry.submitter.dispatcher_id
        )
        await channel.write_typed_frame(
            FrameType.JSON, canonical_bytes(request.model_dump(mode="json")), deadline
        )
        _, payload = await channel.read_typed_frame(
            deadline, {FrameType.JSON.value: ENVELOPE_BYTES}
        )
        read_delivery(payload, "wire", "attached")
        yield channel
    finally:
        channel.close()


async def transfer_header(
    channel: BoundedChannel, ctx: ProtocolContext, item: TransferItem
) -> None:
    request = TransferRequest(op="transfer_in", request_id="wire", item=item)
    await channel.write_typed_frame(
        FrameType.JSON,
        canonical_bytes(request.model_dump(mode="json")),
        ctx.clock.monotonic_ns() + FRAME_PROGRESS_NS,
    )


async def transfer_body(
    channel: BoundedChannel, ctx: ProtocolContext, payload: bytes
) -> None:
    for offset in range(0, len(payload), SECTION_FRAME_BYTES):
        await channel.write_typed_frame(
            FrameType.BYTES,
            payload[offset : offset + SECTION_FRAME_BYTES],
            ctx.clock.monotonic_ns() + FRAME_PROGRESS_NS,
        )


async def transferred(channel: BoundedChannel, ctx: ProtocolContext) -> None:
    _, payload = await channel.read_typed_frame(
        ctx.clock.monotonic_ns() + FRAME_PROGRESS_NS,
        {FrameType.JSON.value: ENVELOPE_BYTES},
    )
    read_delivery(payload, "wire", "transferred")


@pytest.mark.asyncio
async def test_input_transfer_is_durable_before_acknowledgement(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        await runtime.transfer_in(
            handle, input_item("payload", payload), chunks(payload), ctx
        )
        stored = attempt / "input" / "payload"
        file_mode = mode_of(stored)
        directory_mode = mode_of(attempt / "input")
        body = stored.read_bytes()
        kinds = journal_kinds(attempt)
        materialised = journal_fields(attempt)
        log = attempt / "runner.log"
        await until(lambda: '"runtime.local.transfer"' in log.read_text())
        diagnostics = transfer_diagnostics(log)
        entry = await recorded(runtime, "attempt-1")
        assert isinstance(entry, Running)
        spool = list((Path(entry.socket_path).parent / "spool").iterdir())
        pid = await running_pid(runtime, "attempt-1")
        os.kill(pid, signal.SIGKILL)
        assert exited_within(pid, 10)
        after = stored.read_bytes()
        remaining = journal_kinds(attempt)

    assert file_mode == 0o400
    assert directory_mode == 0o700
    assert body == payload == after
    assert kinds.count("transfer.materialised") == 1
    assert remaining.count("transfer.materialised") == 1
    assert materialised == ("payload", digest_of(payload))
    assert [outcome for outcome, _waited in diagnostics] == ["materialised"]
    assert all(isinstance(waited, int) for _outcome, waited in diagnostics)
    assert spool == []


@pytest.mark.asyncio
async def test_transfer_digest_mismatch_is_rejected(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        refused = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("payload", payload, sha256=digest_of(b"other-bytes")),
                chunks(payload),
                ctx,
            )
        )
        names = sorted(path.name for path in (attempt / "input").iterdir())
        kinds = journal_kinds(attempt)

    assert refused.code == "integrity_error"
    assert refused.retryable is False
    assert names == []
    assert "transfer.materialised" not in kinds
    assert kinds.count("transfer.rejected") == 1


@pytest.mark.asyncio
async def test_transfer_name_conflict_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    other = b"other-bytes!!"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        item = input_item("payload", payload)
        await runtime.transfer_in(handle, item, chunks(payload), ctx)
        await runtime.transfer_in(handle, item, chunks(payload), ctx)
        conflict = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", other), chunks(other), ctx
            )
        )
        changed = b"X" * len(payload)
        rejected = await protocol_error(
            runtime.transfer_in(handle, item, chunks(changed), ctx)
        )
        note = b"note-bytes"
        missed = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("note", note, sha256=digest_of(b"not-the-note")),
                chunks(note),
                ctx,
            )
        )
        stored = attempt / "input" / "payload"
        body = stored.read_bytes()
        names = sorted(path.name for path in stored.parent.iterdir())
        text = stored_text(attempt)

    assert conflict.code == "workload_conflict"
    assert rejected.code == "integrity_error"
    assert missed.code == "integrity_error"
    assert names == ["payload"]
    assert body == payload
    assert text.count('"transfer.materialised"') == 1


@pytest.mark.asyncio
async def test_script_transfer_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"script-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        refused = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("payload", payload, kind="script"),
                chunks(payload),
                ctx,
            )
        )
        resource = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("payload", payload, kind="resource"),
                chunks(payload),
                ctx,
            )
        )
        created = (attempt / "input").exists()

    assert refused.code == "transfer_refused"
    assert resource.code == "transfer_refused"
    assert refused.retryable is False
    assert created is False


@pytest.mark.asyncio
async def test_transfer_after_start_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        await runtime.start(handle, ctx)
        refused = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", payload), chunks(payload), ctx
            )
        )
        created = (attempt / "input").exists()

    assert refused.code == "transfer_refused"
    assert created is False


@pytest.mark.asyncio
async def test_accepted_incoming_name_survives_the_next_transfer(
    tmp_path: Path, kora_capture: Capture
) -> None:
    kept = b"kept-bytes"
    nxt = b"next-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        await runtime.transfer_in(
            handle, input_item(".incoming", kept), chunks(kept), ctx
        )
        await runtime.transfer_in(handle, input_item("payload", nxt), chunks(nxt), ctx)
        directory = attempt / "input"
        names = sorted(path.name for path in directory.iterdir())
        kept_body = (directory / ".incoming").read_bytes()
        next_body = (directory / "payload").read_bytes()

    assert names == [".incoming", "payload"]
    assert kept_body == kept
    assert next_body == nxt


@pytest.mark.asyncio
async def test_transfer_does_not_follow_an_input_symlink(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        directory = attempt / "input"
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        outside = attempt / "outside"
        outside.write_bytes(b"kept")
        (directory / "payload").symlink_to(outside)
        refused = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", payload), chunks(payload), ctx
            )
        )
        followed = outside.read_bytes()
        linked = (directory / "payload").is_symlink()

    assert refused.code == "transfer_refused"
    assert followed == b"kept"
    assert linked is True


async def transfer_worker() -> None:
    root = Path(os.environ["KORA_COMMAND_ROOT"])
    sockets = Path(os.environ["KORA_COMMAND_SOCKETS"])
    clock = SystemClock()
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(),
        "transfer-worker",
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
    await runtime.submit(command_workload(runtime, "attempt-1", (binary("true"),)), ctx)
    entry = await recorded(runtime, "attempt-1")
    assert isinstance(entry, Running)
    sys.stdout.write(f"{entry.socket_path}\n")
    sys.stdout.flush()
    signal.pause()


@pytest.mark.asyncio
async def test_observational_attach_cannot_transfer(tmp_path: Path) -> None:
    payload = b"payload-bytes"
    root = state_root(tmp_path)
    with short_directory() as base:
        sockets = base / "sockets"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import asyncio\n"
                "from tests.plugins.runtime.local.test_transfer import "
                "transfer_worker\n"
                "asyncio.run(transfer_worker())\n",
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
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            identity = identity_of(runners[0])
            spool = base / "body"
            spool.write_bytes(payload)
            executor = TransitionExecutor()
            clock = SystemClock()
            try:
                with spool.open("rb") as source:
                    refused = await protocol_error(
                        transfer_input(
                            path.strip(),
                            identity,
                            "dispatcher-2",
                            kernel_source(NativeIdentityReader()),
                            executor,
                            clock,
                            "transfer-1",
                            source.fileno(),
                            input_item("payload", payload),
                            clock.monotonic_ns() + MESSAGE_PROGRESS_NS,
                            close_spool=source.close,
                        )
                    )
            finally:
                await executor.close()
            created = any((root / "attempts").glob("*/input"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            release_commands(root)
            reap_runners(root)

    assert path.strip()
    assert refused.code == "execution_unauthorized"
    assert created is False


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
async def test_start_waits_for_transfer_slot(
    tmp_path: Path, kora_capture: Capture, replay: bool
) -> None:
    payload = b"x" * (8 * SECTION_FRAME_BYTES) + b"y"
    item = input_item("payload", payload)
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        if replay:
            await runtime.transfer_in(handle, item, chunks(payload), ctx)
        entry = await recorded(runtime, "attempt-1")
        assert isinstance(entry, Running)

        async with transfer_channel(runtime, ctx, entry) as channel:
            await transfer_header(channel, ctx, item)
            if replay:
                await transfer_body(channel, ctx, payload[:-1])
            else:
                await until(lambda: (attempt / "input" / INCOMING).exists())
            busy = await protocol_error(runtime.start(handle, ctx))
            candidate_exists = (attempt / "candidate").exists()
            if replay:
                await transfer_body(channel, ctx, b"z")
                rejected = await protocol_error(transferred(channel, ctx))
            else:
                await transfer_body(channel, ctx, payload)
                await transferred(channel, ctx)
                rejected = None
        await runtime.start(handle, ctx)
        await until(lambda: "command_exited" in journal_kinds(attempt))
        anchor = open_local_directory(attempt)
        try:
            records = read_records(anchor)
        finally:
            anchor.close()

    assert busy.code == "workload_busy"
    assert busy.retryable is True
    assert candidate_exists is False
    indices = [index for index, _ in records]
    assert indices == list(range(indices[0], indices[0] + len(indices)))
    suffix = ["transfer.materialised"]
    if replay:
        assert rejected is not None
        assert rejected.code == "integrity_error"
        suffix.append("transfer.rejected")
    assert [kind for _, kind in records][-len(suffix) - 2 :] == [
        *suffix,
        "runner.candidate_started",
        "command_exited",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_authority", ["deadline", "superseded"])
async def test_held_body_loses_transfer_authority(
    tmp_path: Path, kora_capture: Capture, lose_authority: str
) -> None:
    item = input_item("payload", b"x")
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(runtime, "attempt-1", (binary("true"),))
        if lose_authority == "deadline":
            deadline = ExecutionDeadline(
                at=ctx.clock.now() + timedelta(seconds=4),
                scope_kind="attempt",
                scope_id="attempt-1",
                clock_uncertainty=timedelta(0),
                grace=DEADLINE_GRACE,
            )
            ctx = replace(ctx, deadline=deadline)
            workload = workload.model_copy(update={"deadline": deadline})
        handle = await runtime.submit(workload, ctx)
        attempt = attempt_directory(root, runtime, handle.attempt_id)
        entry = await recorded(runtime, handle.attempt_id)
        assert isinstance(entry, Running)
        incoming = attempt / "input" / INCOMING

        async with transfer_channel(runtime, ctx, entry) as channel:
            await transfer_header(channel, ctx, item)
            await until(incoming.exists)
            if lose_authority == "deadline":
                with pytest.raises(ChannelError):
                    await transferred(channel, ctx)
            else:
                key = runtime.registry.key(handle.attempt_id)
                successor = entry.submitter.model_copy(
                    update={"dispatcher_id": "dispatcher-2"}
                )
                await runtime.transition(
                    key,
                    lambda current: takeover_cell(current, entry, successor, False),
                    ctx,
                    "submit",
                )
                await transfer_body(channel, ctx, b"x")
                refused = await protocol_error(transferred(channel, ctx))
                assert refused.code == "submitter_superseded"
        await until(lambda: not incoming.exists())
        async with asyncio.timeout(5):
            while True:
                try:
                    current = await recorded(runtime, handle.attempt_id)
                except KoraProtocolError as error:
                    if error.code != "lineage_unavailable":
                        raise
                    continue
                assert isinstance(current, Running)
                if lose_authority != "deadline" or current.gate_closed:
                    break
        if lose_authority == "deadline":
            async with transfer_channel(runtime, ctx, current) as late:
                await transfer_header(late, ctx, item)
                refused = await protocol_error(transferred(late, ctx))
                assert refused.code == "deadline_expired"
        names = tuple((attempt / "input").iterdir())
        kinds = journal_kinds(attempt)

    assert isinstance(current, Running)
    assert current.gate_closed == (lose_authority == "deadline")
    assert names == ()
    assert "transfer.materialised" not in kinds
    assert "transfer.rejected" not in kinds


@pytest.mark.asyncio
async def test_disconnected_transfer_closes_exact_incoming_descriptor(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path, "attempt")
    anchor = open_local_directory(attempt)
    clock = SystemClock()
    scope = SecretScope(kora_capture.diagnostics)
    command = CommandSession(
        anchor,
        str(attempt / "candidate"),
        scope,
        clock,
        kora_capture.diagnostics.logger(COMPONENT),
    )
    incoming = attempt / "input" / INCOMING

    try:
        async with delivery_server(
            tmp_path, kora_capture, clock=clock, command=command
        ) as live:
            channel = await live.attach()
            try:
                await live.deliver(channel)
                baseline = live_descriptors()
                await transfer_header(channel, live.ctx, input_item("payload", b"x"))
                await until(incoming.exists)
                pending = incoming.stat()
                identity = (pending.st_dev, pending.st_ino)

                def open_incoming() -> set[int]:
                    return {
                        descriptor
                        for descriptor, current in descriptor_identities().items()
                        if current == identity and descriptor not in baseline
                    }

                await until(lambda: bool(open_incoming()))
                owned = open_incoming()
            finally:
                channel.close()
            await until(lambda: not incoming.exists())
            remaining = live_descriptors()
    finally:
        anchor.close()

    assert len(owned) == 1
    assert owned.isdisjoint(remaining)
    assert scope.released
    assert not (attempt / "input" / "payload").exists()


@pytest.mark.asyncio
async def test_runtime_close_settles_parked_spool(
    tmp_path: Path, kora_capture: Capture
) -> None:
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def parked() -> AsyncIterator[bytes]:
        try:
            yield b"a"
            entered.set()
            await asyncio.Event().wait()
            yield b"b"
        finally:
            stopped.set()

    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        entry = await recorded(runtime, handle.attempt_id)
        assert isinstance(entry, Running)
        spool = Path(entry.socket_path).parent / "spool"
        descriptors = live_descriptors()

        async with asyncio.TaskGroup() as tasks:
            operation = tasks.create_task(
                runtime.transfer_in(handle, input_item("payload", b"ab"), parked(), ctx)
            )
            async with asyncio.timeout(5):
                await entered.wait()
                added = live_descriptors() - descriptors
                assert len(added) == 1
                spool_fd = added.pop()
                assert os.fstat(spool_fd).st_nlink == 0
                await runtime.close()
        after = live_descriptors()
        names = tuple(spool.iterdir())

    assert spool_fd not in after
    assert operation.cancelled()
    assert stopped.is_set()
    assert names == ()
    assert not (attempt / "input").exists()


@pytest.mark.asyncio
async def test_definite_native_refusal_releases_spool(
    tmp_path: Path, kora_capture: Capture
) -> None:
    spooled = asyncio.Event()
    resume = asyncio.Event()
    release_native = threading.Event()

    async def parked() -> AsyncIterator[bytes]:
        yield b"a"
        spooled.set()
        await resume.wait()
        yield b"b"

    def hold_native() -> None:
        if not release_native.wait(5):
            raise TimeoutError("test native admission release")

    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        original = live_descriptors()
        executor = runtime.executor

        async with asyncio.TaskGroup() as tasks:
            operation = tasks.create_task(
                protocol_error(
                    runtime.transfer_in(
                        handle, input_item("payload", b"ab"), parked(), ctx
                    )
                )
            )
            try:
                async with asyncio.timeout(5):
                    await spooled.wait()
                    added = live_descriptors() - original
                    assert len(added) == 1
                    spool_fd = added.pop()
                    assert os.fstat(spool_fd).st_nlink == 0
                    for _ in range(executor.admitted):
                        tasks.create_task(executor.run(hold_native))
                    await until(lambda: len(executor.jobs) == executor.admitted)
                    resume.set()
                    refused = await operation
                    after_refusal = live_descriptors()
            finally:
                release_native.set()
                resume.set()

    assert refused.code == "registry_busy"
    assert refused.retryable is True
    assert spool_fd not in after_refusal
    assert not (attempt / "input").exists()


@pytest.mark.asyncio
async def test_uncertain_spool_is_released_during_runtime_shutdown(
    tmp_path: Path, kora_capture: Capture
) -> None:
    spooled = asyncio.Event()
    resume = asyncio.Event()
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)

    async def parked() -> AsyncIterator[bytes]:
        yield b"a"
        spooled.set()
        await resume.wait()
        yield b"b"

    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        pool_stopped = False
        try:
            handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
            original = live_descriptors()

            async def transfer() -> None:
                with pytest.raises(NativeIOSubmissionUncertain):
                    await runtime.transfer_in(
                        handle, input_item("payload", b"ab"), parked(), ctx
                    )

            async with asyncio.TaskGroup() as tasks:
                operation = tasks.create_task(transfer())
                async with asyncio.timeout(5):
                    await spooled.wait()
                    added = live_descriptors() - original
                    assert len(added) == 1
                    spool_fd = added.pop()
                    spool_inode = os.fstat(spool_fd).st_ino
                    runtime.executor.executor.shutdown(wait=False)
                    pool_stopped = True
                    resume.set()
                    await operation
                    retained_inode = os.fstat(spool_fd).st_ino
        finally:
            try:
                if pool_stopped:
                    with pytest.raises(NativeIOUnresolved):
                        await runtime.close()
                else:
                    await runtime.close()
            finally:
                release_commands(root)
                reap_runners(root)
        remaining = live_descriptors()

    assert retained_inode == spool_inode
    assert spool_fd not in remaining
    assert runtime.registry.directory.closed
    assert runtime.sweeps.closing is not None
    assert runtime.sweeps.closing.done()
    assert not (attempt / "input").exists()


class TransferShutdownServer(ControlServer):
    def recheck(self, ready: asyncio.Event) -> None:
        self._recheck_ready: asyncio.Event = ready

    async def retain_disposal(self, dispose: Callable[[], None]) -> None:
        self._executor.executor.shutdown(wait=False)
        await self._transfer_native(lambda: None, dispose)

    def recheck_done(self) -> bool:
        return self._ticking is not None and self._ticking.done()

    async def _tick(self) -> None:
        await self._recheck_ready.wait()
        await self._reverify()


@pytest.mark.asyncio
async def test_runner_drains_deferred_cleanup_after_socket_check_failure(
    tmp_path: Path, kora_capture: Capture
) -> None:
    cleanup = threading.Event()
    ready = asyncio.Event()
    descriptor = os.open(tmp_path / "owned", os.O_RDWR | os.O_CREAT, 0o600)

    def dispose() -> None:
        os.close(descriptor)
        cleanup.set()

    with short_directory() as base:
        directory = private_directory(base, "s")
        bound = rebound(directory, "control.sock")
        registry = Registry(
            open_local_directory(directory), "domain-1", lambda: "a" * 32
        )
        reader = NativeIdentityReader()
        server = TransferShutdownServer(
            bound,
            directory,
            "control.sock",
            SystemClock(),
            kernel_source(reader),
            kora_capture.diagnostics.logger(COMPONENT),
            registry,
            registry.key("attempt-1"),
            identity_of(os.getpid()),
            reader,
        )
        server.recheck(ready)
        server.start()

        try:
            with pytest.raises(NativeIOSubmissionUncertain):
                await server.retain_disposal(dispose)
            ready.set()
            await until(server.recheck_done)
            retained = not cleanup.is_set()
        finally:
            try:
                with pytest.raises(NativeIOUnresolved):
                    await server.close()
                released_by_server = bound.directory.closed
                resources_settled = server.resources_settled
            finally:
                registry.directory.close()
                if not cleanup.is_set():
                    os.close(descriptor)
                bound.close()

    assert retained
    assert cleanup.is_set()
    assert released_by_server
    assert resources_settled


@pytest.mark.asyncio
async def test_empty_chunks_allow_runtime_shutdown(
    tmp_path: Path, kora_capture: Capture
) -> None:
    started = asyncio.Event()
    exhausted = asyncio.Event()

    async def empty_chunks() -> AsyncIterator[bytes]:
        started.set()
        for _ in range(4096):
            yield b""
        exhausted.set()
        await asyncio.Event().wait()

    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")

        async with asyncio.TaskGroup() as tasks:
            operation = tasks.create_task(
                runtime.transfer_in(
                    handle, input_item("payload", b""), empty_chunks(), ctx
                )
            )
            async with asyncio.timeout(5):
                await started.wait()
                await runtime.close()

    assert operation.cancelled()
    assert not exhausted.is_set()
    assert not (attempt / "input").exists()


@pytest.mark.asyncio
async def test_transfer_refuses_symlinked_spool(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        entry = await recorded(runtime, handle.attempt_id)
        assert isinstance(entry, Running)
        outside = tmp_path / "spool-target"
        outside.mkdir(mode=0o755)
        outside.chmod(0o755)
        spool = Path(entry.socket_path).parent / "spool"
        spool.symlink_to(outside, target_is_directory=True)

        refused = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", payload), chunks(payload), ctx
            )
        )
        names = tuple(outside.iterdir())
        mode = mode_of(outside)

    assert refused.code == "runner_unreachable"
    assert names == ()
    assert mode == 0o755
    assert not (attempt / "input").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["fifo", "hardlink", "oversized"])
async def test_unrecorded_collision_refuses_without_opening(
    tmp_path: Path, kora_capture: Capture, collision: str
) -> None:
    payload = b"payload"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        directory = private_directory(attempt, "input")
        target = directory / "payload"
        if collision == "fifo":
            os.mkfifo(target, 0o400)
        elif collision == "hardlink":
            outside = attempt / "outside"
            outside.write_bytes(payload)
            target.hardlink_to(outside)
        else:
            target.write_bytes(payload + b"extra")
        previous = target.stat()

        refused = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", payload), chunks(payload), ctx
            )
        )
        current = target.stat()
        names = sorted(path.name for path in directory.iterdir())
        kinds = journal_kinds(attempt)

    expected = "workload_conflict" if collision == "oversized" else "transfer_refused"
    assert refused.code == expected
    assert current.st_ino == previous.st_ino
    assert current.st_size == previous.st_size
    assert names == ["payload"]
    assert "transfer.materialised" not in kinds


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_journal", ["symlink", "mode"])
async def test_transfer_removes_placement_when_journal_refuses(
    tmp_path: Path, kora_capture: Capture, invalid_journal: str
) -> None:
    payload = b"payload"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        target = tmp_path / "outside-journal"
        target.write_bytes(b"outside")
        journal = attempt / "journal.jsonl"
        journal.unlink(missing_ok=True)
        if invalid_journal == "symlink":
            journal.symlink_to(target)
        else:
            journal.write_bytes(b"wrong-mode")
            journal.chmod(0o644)

        refused = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", payload), chunks(payload), ctx
            )
        )
        blocked = await protocol_error(runtime.start(handle, ctx))
        names = tuple((attempt / "input").iterdir())
        contents = target.read_bytes()

    assert refused.code == "journal_unavailable"
    assert blocked.code == "journal_unavailable"
    assert names == ()
    assert contents == b"outside"


@pytest.mark.asyncio
@pytest.mark.host_fixture
@pytest.mark.parametrize("failure", ["open", "body"])
async def test_fenced_journal_refuses_transfer(
    tmp_path: Path, kora_capture: Capture, failure: str
) -> None:
    payload = b"x" * (SECTION_FRAME_BYTES + 1)
    item = input_item("payload", payload)
    small = private_directory(
        prepared("small-filesystem"), f"{PATH_MARKER}-{os.getpid()}-transfer"
    )
    root = private_directory(small, "state")
    ctx = runtime_context(kora_capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
            entry = await recorded(runtime, handle.attempt_id)
            assert isinstance(entry, Running)
            incoming = attempt / "input" / INCOMING

            if failure == "open":
                fill(attempt)
                refused = await protocol_error(
                    runtime.transfer_in(handle, item, chunks(payload), ctx)
                )
                assert refused.code == "journal_unavailable"
                assert refused.retryable is True
            else:
                async with transfer_channel(runtime, ctx, entry) as channel:
                    await transfer_header(channel, ctx, item)
                    await until(incoming.exists)
                    fill(attempt)
                    await transfer_body(channel, ctx, payload[:-1])
                    async with asyncio.timeout(3):
                        while True:
                            observed_fence = await protocol_error(
                                runtime.start(handle, ctx)
                            )
                            if observed_fence.code == "journal_unavailable":
                                break
                            assert observed_fence.code == "workload_busy"
                await until(lambda: not incoming.exists())
            (attempt / "filler").unlink()
            blocked = await protocol_error(runtime.start(handle, ctx))
            refused_again = await protocol_error(
                runtime.transfer_in(handle, item, chunks(payload), ctx)
            )
            present = (attempt / "input").exists() and any(
                (attempt / "input").iterdir()
            )
            text = stored_text(attempt)
        finally:
            release_commands(root)
            await runtime.close()
            reap_runners(root)
            shutil.rmtree(small)

    assert blocked.code == "journal_unavailable"
    assert blocked.retryable is True
    assert refused_again.code == "journal_unavailable"
    assert refused_again.retryable is True
    assert present is False
    assert "transfer.materialised" not in text
