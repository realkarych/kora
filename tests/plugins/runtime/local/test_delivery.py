import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import weakref
from collections.abc import AsyncGenerator, Generator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
from kora.ir.json import JsonObjectField
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import Level, LoggingSettings
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    FrameType,
    SectionDeclaration,
    SectionedHeader,
    control_request,
    read_error_envelope,
    workload_digest,
)
from kora.plugins.runtime.local.serving import FIRST_FRAME_NS, decoded_workload
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import Workload
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.filesystem import read_private_file
from kora.runner.identity import (
    GroupObservation,
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
)
from kora.runner.peers import kernel_source
from kora.runner.registry import (
    HEAD_LIMIT,
    Running,
    Submitter,
    with_entry,
    write_atomically,
)
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_delivery import delivery_server
from tests.support.local_peers import (
    CONDITION_SECONDS,
    FAR_NS,
    GatedSource,
    asked_status,
    client,
    identity_of,
    served,
    sleeping_process,
    until,
)
from tests.support.local_runners import (
    open_spawning,
    reap_runners,
    runner_pids,
    short_directory,
    submitted,
    zombie_children,
)
from tests.support.local_runtime import open_descriptors, runtime_context, state_root

pytestmark = pytest.mark.asyncio


@pytest.fixture
def kora_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "delivery",
        utc_now,
    ) as capture:
        yield capture


async def test_starting_entry_resolves_with_workload_and_repeat(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        channel = await live.attach()
        try:
            await live.deliver(channel)
            await live.deliver(channel)
            status = await client(live.entry.socket_path, live.ctx.clock)
            try:
                observed = await asked_status(status, live.ctx.clock)
            finally:
                status.close()
        finally:
            channel.close()

        assert observed.holds_workload
        assert observed.status.state == "starting"
        assert observed.status.phase == "preparation"


@pytest.mark.parametrize("change", ["dispatcher", "runner"])
async def test_control_channel_rejects_impostors_at_attach(
    tmp_path: Path, kora_capture: Capture, change: str
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        dispatcher = "foreign" if change == "dispatcher" else live.work.dispatcher_id
        if change == "runner":
            await live.write(
                live.entry.model_copy(
                    update={
                        "runner": live.entry.runner.model_copy(
                            update={"start_time": live.entry.runner.start_time + 1}
                        )
                    }
                )
            )

        with pytest.raises(ChannelError):
            await live.attach(dispatcher)


@pytest.mark.parametrize("change", ["submitter", "gate", "digest", "content"])
async def test_control_frames_refuse_changed_authority_or_content(
    tmp_path: Path, kora_capture: Capture, change: str
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        channel = await live.attach()
        work = live.work
        if change == "submitter":
            await live.write(
                live.entry.model_copy(
                    update={
                        "submitter": Submitter(
                            dispatcher_id="replacement",
                            process=live.entry.submitter.process,
                        )
                    }
                )
            )
        elif change == "gate":
            await live.write(live.entry.model_copy(update={"gate_closed": True}))
        elif change == "digest":
            await live.write(
                live.entry.model_copy(update={"workload_digest": "sha256:" + "f" * 64})
            )
        else:
            work = work.model_copy(
                update={
                    "resolved_config": JsonObjectField(
                        {"changed": "unregistered-delivery-marker"}
                    )
                }
            )
        expected = {
            "submitter": "submitter_superseded",
            "gate": "deadline_expired",
        }.get(change, "workload_conflict")
        try:
            with pytest.raises(KoraProtocolError) as refused:
                await live.deliver(channel, work)
        finally:
            channel.close()

        assert refused.value.code == expected
        assert refused.value.details == {}


async def test_control_frames_accept_logically_equal_new_dispatcher(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        first = await live.attach()
        try:
            await live.deliver(first)
        finally:
            first.close()
        replacement = live.work.model_copy(update={"dispatcher_id": "replacement"})
        await live.write(
            live.entry.model_copy(
                update={
                    "submitter": Submitter(
                        dispatcher_id="replacement",
                        process=live.entry.submitter.process,
                    )
                }
            )
        )
        second = await live.attach("replacement")
        try:
            await live.deliver(second, replacement)
        finally:
            second.close()

        assert workload_digest(replacement) == workload_digest(live.work)


class GatedDecoder:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self, section: bytearray) -> Workload:
        self.entered.set()
        if not self.release.wait(CONDITION_SECONDS):
            raise TimeoutError("decode barrier was not released")
        try:
            return decoded_workload(section)
        finally:
            self.finished.set()


async def test_local_runtime_releases_resources_only_after_decode(
    tmp_path: Path, kora_capture: Capture
) -> None:
    decoder = GatedDecoder()
    async with delivery_server(tmp_path, kora_capture, decoder=decoder) as live:
        first = await live.attach()
        second = await live.attach()
        delivering = asyncio.create_task(live.deliver(first))
        closing: asyncio.Task[None] | None = None
        try:
            await until(decoder.entered.is_set)
            first.close()
            with pytest.raises(KoraProtocolError) as busy:
                await live.deliver(second)
            closing = asyncio.create_task(live.server.close())
            await until(lambda: first.closed)

            assert busy.value.code == "workload_busy"
            assert not decoder.finished.is_set()
            assert not closing.done()
            decoder.release.set()
            await closing
            assert decoder.finished.is_set()
            assert live.server.connections == 0
        finally:
            decoder.release.set()
            first.close()
            second.close()
            await asyncio.gather(delivering, return_exceptions=True)
            if closing is not None:
                await closing


async def test_control_channel_rejects_seventeenth_classifier(
    tmp_path: Path, kora_capture: Capture
) -> None:
    source = GatedSource(kernel_source(NativeIdentityReader()))
    clock = StepClock()
    with short_directory() as base:
        async with served(base / "sockets", clock, source, kora_capture) as (
            server,
            path,
        ):
            connections = [await client(path, clock) for _ in range(16)]
            try:
                await until(lambda: server.connections == 16)
                seventeenth = await client(path, clock)
                try:
                    await seventeenth.expect_eof(clock.monotonic_ns() + FAR_NS)
                finally:
                    seventeenth.close()

                assert server.connections == 16
                assert not source.gate.is_set()
            finally:
                for channel in connections:
                    channel.close()
                source.gate.set()


async def test_control_channel_allows_submitter_beside_eight_observers(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = StepClock()
    async with delivery_server(tmp_path, kora_capture, clock=clock) as live:
        with sleeping_process() as other:
            await live.write(
                live.entry.model_copy(
                    update={
                        "submitter": Submitter(
                            dispatcher_id="other", process=identity_of(other.pid)
                        )
                    }
                )
            )
            observers = [await client(live.entry.socket_path, clock) for _ in range(8)]
            try:
                await until(
                    lambda: sum(not future.done() for _, future in clock.sleepers) == 17
                )
                await live.write(live.entry)
                channel = await live.attach()
                try:
                    await live.deliver(channel)
                finally:
                    channel.close()

                assert live.server.connections >= 8
                for observer in observers:
                    assert not observer.closed
            finally:
                for observer in observers:
                    observer.close()


@pytest.mark.parametrize(
    "state", ["dead", "zombie", "paused", "same-process", "foreign-namespace"]
)
async def test_starting_entry_resolves_only_after_comparable_submitter_exit(
    tmp_path: Path, kora_capture: Capture, state: str
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        with sleeping_process() as peer:
            identity = identity_of(peer.pid)
            if state == "same-process":
                identity = live.entry.submitter.process
            elif state == "foreign-namespace":
                identity = identity.model_copy(
                    update={"pid_namespace": "another-namespace"}
                )
            elif state == "paused":
                os.kill(peer.pid, signal.SIGSTOP)
            else:
                peer.kill()
                if state == "dead":
                    peer.wait(timeout=CONDITION_SECONDS)
                else:
                    await until(lambda: peer.pid in zombie_children())
            previous = Submitter(dispatcher_id="previous", process=identity)
            await live.write(live.entry.model_copy(update={"submitter": previous}))

            if state == "foreign-namespace":
                with pytest.raises(KoraProtocolError) as refused:
                    await live.runtime.submit(live.work, live.ctx)
                assert refused.value.code == "runner_unreachable"
            else:
                handle = await live.runtime.submit(live.work, live.ctx)
                assert handle.attempt_id == live.work.attempt_id
            status = await client(live.entry.socket_path, live.ctx.clock)
            try:
                observed = await asked_status(status, live.ctx.clock)
            finally:
                status.close()
            entry = await live.runtime.entry(
                live.runtime.registry.key(live.work.attempt_id)
            )

            taken_over = state in {"dead", "zombie"}
            assert observed.holds_workload is taken_over
            assert isinstance(entry, Running)
            assert entry.submitter == (live.entry.submitter if taken_over else previous)


async def test_starting_entry_resolves_after_real_runner_readiness(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        work = submitted(runtime, "attempt-1")
        try:
            handle = await runtime.submit(work, ctx)
            repeated = await runtime.submit(work, ctx)
            entry = await runtime.entry(runtime.registry.key(work.attempt_id))
            assert isinstance(entry, Running)
            channel = await client(entry.socket_path, ctx.clock)
            try:
                observed = await asked_status(channel, ctx.clock)
            finally:
                channel.close()
        finally:
            await runtime.close()
            reap_runners(root)

        assert repeated == handle
        assert observed.holds_workload
        assert entry.workload_digest == workload_digest(work)
        assert runner_pids(root) == ()


@pytest.mark.parametrize("change", ["submitter", "gate", "runner"])
async def test_control_frames_recheck_authority_after_decode(
    tmp_path: Path, kora_capture: Capture, change: str
) -> None:
    decoder = GatedDecoder()
    async with delivery_server(tmp_path, kora_capture, decoder=decoder) as live:
        channel = await live.attach()
        delivering = asyncio.create_task(live.deliver(channel))
        try:
            await until(decoder.entered.is_set)
            if change == "submitter":
                changed = live.entry.model_copy(
                    update={
                        "submitter": Submitter(
                            dispatcher_id="replacement",
                            process=live.entry.submitter.process,
                        )
                    }
                )
            elif change == "gate":
                changed = live.entry.model_copy(update={"gate_closed": True})
            else:
                changed = live.entry.model_copy(
                    update={
                        "runner": live.entry.runner.model_copy(
                            update={"start_time": live.entry.runner.start_time + 1}
                        )
                    }
                )
            await live.write(changed)
            decoder.release.set()

            if change == "runner":
                with pytest.raises(ChannelError):
                    await delivering
            else:
                with pytest.raises(KoraProtocolError) as refused:
                    await delivering
                assert refused.value.code == (
                    "deadline_expired" if change == "gate" else "submitter_superseded"
                )
        finally:
            decoder.release.set()
            channel.close()
            await asyncio.gather(delivering, return_exceptions=True)


@pytest.mark.parametrize(
    "failure", ["invalid-json", "digest", "short", "disconnect", "extra-section"]
)
async def test_local_runtime_releases_resources_after_refused_delivery(
    tmp_path: Path, kora_capture: Capture, failure: str
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        channel = await live.attach()
        payload = b'{"private":"unregistered-delivery-marker"'
        declaration = SectionDeclaration(
            name="workload",
            length=len(payload) + 1 if failure == "short" else len(payload),
            sha256="0" * 64
            if failure == "digest"
            else hashlib.sha256(payload).hexdigest(),
        )
        sections = (
            (declaration, declaration) if failure == "extra-section" else (declaration,)
        )
        header = SectionedHeader(
            op="accept_workload", request_id="invalid", sections=sections
        )
        deadline = live.ctx.clock.monotonic_ns() + FAR_NS
        try:
            await channel.write_typed_frame(
                FrameType.JSON,
                canonical_bytes(header.model_dump(mode="json")),
                deadline,
            )
            if failure != "disconnect" and failure != "extra-section":
                await channel.write_typed_frame(FrameType.BYTES, payload, deadline)
            if failure in {"disconnect", "short"}:
                channel.close()
            else:
                await channel.expect_eof(deadline)
        finally:
            channel.close()
        await until(lambda: live.server.connections == 0)

        following = await live.attach()
        try:
            await live.deliver(following)
        finally:
            following.close()

        await until(lambda: live.server.connections == 0)


async def test_control_frames_status_and_unknown_after_attach(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        channel = await live.attach()
        deadline = live.ctx.clock.monotonic_ns() + FAR_NS
        try:
            await live.deliver(channel)
            status = await asked_status(channel, live.ctx.clock)
            await channel.write_typed_frame(
                FrameType.JSON,
                control_request("private-unknown-name", "unknown-1"),
                deadline,
            )
            _, answer = await channel.read_typed_frame(
                deadline, {FrameType.JSON.value: ENVELOPE_BYTES}
            )
            request_id, error = read_error_envelope(answer)
        finally:
            channel.close()

        assert status.holds_workload
        assert request_id == "unknown-1"
        assert error.code == "operation_unknown"
        assert error.details == {}
        assert b"private-unknown-name" not in answer


class CountingDecoder:
    def __init__(self) -> None:
        self.calls = 0
        self.references: list[weakref.ReferenceType[Workload]] = []

    def __call__(self, section: bytearray) -> Workload:
        self.calls += 1
        work = decoded_workload(section)
        self.references.append(weakref.ref(work))
        return work


async def test_control_frames_repeat_retains_original_workload(
    tmp_path: Path, kora_capture: Capture
) -> None:
    decoder = CountingDecoder()
    async with delivery_server(tmp_path, kora_capture, decoder=decoder) as live:
        first = await live.attach()
        try:
            await live.deliver(first)
            await live.deliver(first)
        finally:
            first.close()
        replacement = live.work.model_copy(update={"dispatcher_id": "replacement"})
        await live.write(
            live.entry.model_copy(
                update={
                    "submitter": Submitter(
                        dispatcher_id="replacement",
                        process=live.entry.submitter.process,
                    )
                }
            )
        )
        second = await live.attach("replacement")
        try:
            await live.deliver(second, replacement)
        finally:
            second.close()
        await until(lambda: live.server.connections == 0)

        assert decoder.calls == 2
        assert decoder.references[0]() is not None
        assert decoder.references[1]() is None
        await live.server.close()
        assert decoder.references[0]() is None


class GatedIdentityReader:
    def __init__(self) -> None:
        self.native = NativeIdentityReader()
        self.entered = threading.Event()
        self.release = threading.Event()

    def read(self, pid: int) -> IdentityObservation:
        self.entered.set()
        if not self.release.wait(CONDITION_SECONDS):
            raise TimeoutError("identity barrier was not released")
        return self.native.read(pid)

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        return self.native.group(process_group_id, deadline_ns)


async def test_control_channel_keeps_original_deadline_during_attach_checks(
    tmp_path: Path, kora_capture: Capture
) -> None:
    reader = GatedIdentityReader()
    clock = StepClock()
    async with delivery_server(
        tmp_path, kora_capture, clock=clock, identities=reader
    ) as live:
        attaching = asyncio.create_task(live.attach())
        try:
            await until(reader.entered.is_set)
            clock.advance(FIRST_FRAME_NS)

            with pytest.raises(ChannelError):
                await attaching
            assert live.server.connections == 1
            reader.release.set()
            await until(lambda: live.server.connections == 0)
            status = await client(live.entry.socket_path, clock)
            try:
                observed = await asked_status(status, clock)
            finally:
                status.close()
            assert not observed.holds_workload
        finally:
            reader.release.set()
            await asyncio.gather(attaching, return_exceptions=True)


@pytest.mark.parametrize("close_runtime", [False, True])
async def test_local_runtime_releases_cancelled_worker_delivery(
    tmp_path: Path, kora_capture: Capture, close_runtime: bool
) -> None:
    decoder = GatedDecoder()
    async with delivery_server(tmp_path, kora_capture, decoder=decoder) as live:
        delivering = asyncio.create_task(live.runtime.submit(live.work, live.ctx))
        try:
            await until(decoder.entered.is_set)
            if close_runtime:
                await live.runtime.close()
            else:
                delivering.cancel()

            with pytest.raises(asyncio.CancelledError):
                await delivering
            assert not decoder.finished.is_set()
            assert live.server.connections == 1
            decoder.release.set()
            await until(lambda: live.server.connections == 0)
            assert decoder.finished.is_set()
            assert not live.runtime.executor.jobs
        finally:
            decoder.release.set()
            await asyncio.gather(delivering, return_exceptions=True)


async def test_starting_entry_resolves_concurrent_takeovers_once(
    tmp_path: Path, kora_capture: Capture
) -> None:
    decoder = CountingDecoder()
    async with delivery_server(tmp_path, kora_capture, decoder=decoder) as live:
        with sleeping_process() as peer:
            previous = Submitter(
                dispatcher_id="previous", process=identity_of(peer.pid)
            )
            peer.kill()
            peer.wait(timeout=CONDITION_SECONDS)
            await live.write(live.entry.model_copy(update={"submitter": previous}))
            rival = live.work.model_copy(update={"dispatcher_id": "rival"})

            handles = await asyncio.gather(
                live.runtime.submit(live.work, live.ctx),
                live.runtime.submit(rival, live.ctx),
            )
            await until(lambda: live.server.connections == 0)
            entry = await live.runtime.entry(
                live.runtime.registry.key(live.work.attempt_id)
            )

            assert handles[0] == handles[1]
            assert decoder.calls == 1
            assert isinstance(entry, Running)
            assert entry.submitter.dispatcher_id in {live.work.dispatcher_id, "rival"}


async def test_control_frames_expiry_during_decode_closes_sticky_gate(
    tmp_path: Path, kora_capture: Capture
) -> None:
    decoder = GatedDecoder()
    clock = StepClock()
    async with delivery_server(
        tmp_path, kora_capture, clock=clock, decoder=decoder
    ) as live:
        deadline = ExecutionDeadline(
            at=clock.now() + timedelta(seconds=1),
            scope_kind="attempt",
            scope_id="attempt-1",
            clock_uncertainty=timedelta(0),
            grace=timedelta(seconds=30),
        )
        work = live.work.model_copy(update={"deadline": deadline})
        await live.write(
            live.entry.model_copy(
                update={"deadline": deadline, "workload_digest": workload_digest(work)}
            )
        )
        channel = await live.attach()
        delivering = asyncio.create_task(live.deliver(channel, work))
        try:
            await until(decoder.entered.is_set)
            clock.advance(1_000_000_000)
            decoder.release.set()

            with pytest.raises(KoraProtocolError) as refused:
                await delivering
            entry = await live.runtime.entry(live.runtime.registry.key(work.attempt_id))

            assert refused.value.code == "deadline_expired"
            assert isinstance(entry, Running)
            assert entry.gate_closed
        finally:
            decoder.release.set()
            channel.close()
            await asyncio.gather(delivering, return_exceptions=True)


@pytest_asyncio.fixture(autouse=True)
async def delivery_resource_balance(kora_capture: Capture) -> AsyncGenerator[None]:
    descriptors = open_descriptors()
    tasks = asyncio.all_tasks() - {asyncio.current_task()}
    threads = {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("kora-runtime-local")
    }
    zombies = zombie_children()

    yield

    assert open_descriptors() == descriptors
    assert asyncio.all_tasks() - {asyncio.current_task()} == tasks
    assert (
        {
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("kora-runtime-local")
        }
        == threads
        == set()
    )
    assert zombie_children() == zombies
    records = kora_capture.finish()
    assert "unregistered-delivery-marker" not in str(records)
    assert all(
        not {"pid", "path", "identity", "exception", "workload", "sha256"}
        & record.keys()
        for record in records
        if record.get("event") == "runtime.local.runner.auth_refused"
    )


INHERITED_PEER = """
import hashlib
import json
import json
import os
import socket
import struct
import sys

peer = socket.socket(socket.AF_UNIX)
sys.stdout.write(str(os.getpid()) + "\\n")
sys.stdout.flush()
sys.stdin.buffer.read(1)
peer.connect(sys.argv[1])
before_attach = sys.argv[2] == "before"
if before_attach:
    sys.stdout.write("connected\\n")
    sys.stdout.flush()
    sys.stdin.buffer.read(1)
else:
    request = json.dumps(
        {"op": "attach", "request_id": "attach", "dispatcher_id": "old"}
    ).encode()
    peer.sendall(struct.pack("!IB", len(request), ord("J")) + request)
    header = peer.recv(5, socket.MSG_WAITALL)
    assert len(header) == 5
    response = peer.recv(struct.unpack("!IB", header)[0], socket.MSG_WAITALL)
    assert json.loads(response)["result"] == "attached"
child = os.fork()
if child:
    sys.stdout.write(str(child) + "\\n")
    sys.stdout.flush()
    os._exit(0)
sys.stdin.buffer.read(1)
if before_attach:
    request = json.dumps({
        "op": "attach", "request_id": "attach", "dispatcher_id": "old"
    }).encode()
else:
    request = json.dumps({
        "op": "accept_workload", "request_id": "delivery",
        "sections": [{
            "name": "workload", "length": 0,
            "sha256": hashlib.sha256(b"").hexdigest()
        }]
    }).encode()
peer.sendall(struct.pack("!IB", len(request), ord("J")) + request)
header = peer.recv(5, socket.MSG_WAITALL)
response = b"closed"
if header:
    response = peer.recv(struct.unpack("!IB", header)[0], socket.MSG_WAITALL)
sys.stdout.buffer.write(response + b"\\n")
sys.stdout.flush()
peer.close()
os._exit(0)
"""


@pytest.mark.parametrize(
    ("superseded", "before_attach"), [(False, False), (True, False), (False, True)]
)
async def test_control_channel_rejects_dead_submitter_inherited_socket(
    tmp_path: Path, kora_capture: Capture, superseded: bool, before_attach: bool
) -> None:
    decoder = CountingDecoder()
    clock = StepClock()
    async with delivery_server(
        tmp_path, kora_capture, clock=clock, decoder=decoder
    ) as live:
        peer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                INHERITED_PEER,
                live.entry.socket_path,
                "before" if before_attach else "after",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        assert peer.stdin is not None
        assert peer.stdout is not None
        child: int | None = None
        try:
            assert int(await asyncio.to_thread(peer.stdout.readline)) == peer.pid
            previous = Submitter(dispatcher_id="old", process=identity_of(peer.pid))
            await live.write(live.entry.model_copy(update={"submitter": previous}))
            peer.stdin.write(b"a")
            peer.stdin.flush()
            if before_attach:
                assert await asyncio.to_thread(peer.stdout.readline) == b"connected\n"
                await until(lambda: len(pending_timers(clock)) == 3)
                peer.stdin.write(b"f")
                peer.stdin.flush()
            child = int(await asyncio.to_thread(peer.stdout.readline))
            await asyncio.to_thread(peer.wait, CONDITION_SECONDS)
            if superseded:
                await live.write(live.entry)

            peer.stdin.write(b"d")
            peer.stdin.flush()
            response = (await asyncio.to_thread(peer.stdout.readline)).strip()
            await until(lambda: live.server.connections == 0)

            if superseded:
                request_id, error = read_error_envelope(response)
                assert (request_id, error.code) == ("delivery", "submitter_superseded")
            else:
                assert response == b"closed"
            assert decoder.calls == 0
        finally:
            peer.stdin.close()
            peer.stdout.close()
            if peer.poll() is None:
                peer.kill()
                peer.wait(timeout=CONDITION_SECONDS)
            if child is not None:
                await until(
                    lambda: (
                        NativeIdentityReader().read(child).state is IdentityState.ABSENT
                    )
                )

    if not superseded:
        await until(lambda: bool(kora_capture.reader.records()))
        events = [
            record
            for record in kora_capture.reader.records()
            if record.get("event") == "runtime.local.runner.auth_refused"
        ]
        assert [
            (event.get("reason"), event.get("count"), event.get("level"))
            for event in events
        ] == [("exited", 1, "WARNING")]


def pending_timers(clock: StepClock) -> set[asyncio.Future[None]]:
    return {future for _, future in clock.sleepers if not future.done()}


@pytest.mark.parametrize("trickle", [False, True])
async def test_local_runtime_releases_workload_slot_after_receive_deadline(
    tmp_path: Path, kora_capture: Capture, trickle: bool
) -> None:
    clock = StepClock()
    async with delivery_server(tmp_path, kora_capture, clock=clock) as live:
        channel = await live.attach()
        before = pending_timers(clock)
        declaration = SectionDeclaration(name="workload", length=1000, sha256="0" * 64)
        header = SectionedHeader(
            op="accept_workload", request_id="timed", sections=(declaration,)
        )
        try:
            await channel.write_typed_frame(
                FrameType.JSON,
                canonical_bytes(header.model_dump(mode="json")),
                clock.monotonic_ns() + FAR_NS,
            )
            await until(lambda before=before: bool(pending_timers(clock) - before))
            if trickle:
                for _ in range(14):
                    before = pending_timers(clock)
                    clock.advance(4_000_000_000)
                    await channel.write_typed_frame(
                        FrameType.BYTES, b" ", clock.monotonic_ns() + FAR_NS
                    )
                    await until(
                        lambda before=before: bool(pending_timers(clock) - before)
                    )
                clock.advance(4_000_000_000)
            else:
                clock.advance(FIRST_FRAME_NS)

            await channel.expect_eof(clock.monotonic_ns() + FAR_NS)
            await until(lambda: live.server.connections == 0)
            following = await live.attach()
            try:
                await live.deliver(following)
            finally:
                following.close()
        finally:
            channel.close()


async def test_registry_rejects_missing_original_digest_without_backfill(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with delivery_server(tmp_path, kora_capture) as live:
        key = live.runtime.registry.key(live.work.attempt_id)
        encoded = with_entry(key, live.entry)
        document = json.loads(encoded)
        del document["entry"]["workload_digest"]
        legacy = json.dumps(document).encode()
        write_atomically(
            live.runtime.registry.directory,
            f"{key.name}.json",
            legacy,
            "e" * 32,
            replace=True,
        )

        with pytest.raises(KoraProtocolError) as refused:
            await live.runtime.submit(live.work, live.ctx)
        after = read_private_file(
            live.runtime.registry.directory, f"{key.name}.json", HEAD_LIMIT
        )

        assert refused.value.code == "lineage_unavailable"
        assert after == legacy
        assert live.server.connections == 0


@pytest.mark.parametrize(
    "case", ["extra", "oversized", "foreign", "changed-runner", "changed-submitter"]
)
async def test_control_channel_checks_attach_after_classification(
    tmp_path: Path, kora_capture: Capture, case: str
) -> None:
    clock = StepClock()
    async with delivery_server(tmp_path, kora_capture, clock=clock) as live:
        channel = await client(live.entry.socket_path, clock)
        try:
            await until(lambda: len(pending_timers(clock)) == 3)
            document: dict[str, object] = {
                "op": "attach",
                "request_id": "attach",
                "dispatcher_id": live.work.dispatcher_id,
            }
            if case == "extra":
                document["private"] = "unregistered-delivery-marker"
            elif case == "oversized":
                document["dispatcher_id"] = "x" * 8192
            elif case == "changed-runner":
                await live.write(
                    live.entry.model_copy(
                        update={
                            "runner": live.entry.runner.model_copy(
                                update={"start_time": live.entry.runner.start_time + 1}
                            )
                        }
                    )
                )
            elif case == "changed-submitter":
                await live.write(
                    live.entry.model_copy(
                        update={
                            "submitter": Submitter(
                                dispatcher_id="replacement",
                                process=live.entry.submitter.process,
                            )
                        }
                    )
                )
            with sleeping_process() as other:
                if case == "foreign":
                    await live.write(
                        live.entry.model_copy(
                            update={
                                "submitter": Submitter(
                                    dispatcher_id=live.work.dispatcher_id,
                                    process=identity_of(other.pid),
                                )
                            }
                        )
                    )

                early_close: ChannelFailure | None = None
                try:
                    await channel.write_typed_frame(
                        FrameType.JSON,
                        json.dumps(document).encode(),
                        clock.monotonic_ns() + FAR_NS,
                    )
                    await channel.expect_eof(clock.monotonic_ns() + FAR_NS)
                except ChannelError as error:
                    early_close = error.reason
                except ConnectionResetError:
                    early_close = ChannelFailure.CLOSED
                if early_close is not None:
                    assert case == "oversized"
                    assert early_close is ChannelFailure.CLOSED

        finally:
            channel.close()
    await until(lambda: bool(kora_capture.reader.records()))
    records = kora_capture.reader.records()
    events = [
        record
        for record in records
        if record.get("event") == "runtime.local.runner.auth_refused"
    ]

    expected = (
        "malformed"
        if case in {"extra", "oversized"}
        else "head_changed"
        if case == "changed-runner"
        else "not_submitter"
    )
    assert [
        (event.get("reason"), event.get("count"), event.get("level"))
        for event in events
    ] == [(expected, 1, "WARNING")]
    assert all(
        not {"pid", "path", "identity", "exception", "workload"} & event.keys()
        for event in events
    )
    assert "unregistered-delivery-marker" not in str(records)
    assert str(tmp_path) not in str(records)


@pytest.mark.parametrize("expire", [False, True])
async def test_control_frames_recheck_gate_after_receiving(
    tmp_path: Path, kora_capture: Capture, expire: bool
) -> None:
    clock = StepClock()
    async with delivery_server(tmp_path, kora_capture, clock=clock) as live:
        work = live.work
        entry = live.entry
        if expire:
            deadline = ExecutionDeadline(
                at=clock.now() + timedelta(seconds=1),
                scope_kind="attempt",
                scope_id="attempt-1",
                clock_uncertainty=timedelta(0),
                grace=timedelta(seconds=30),
            )
            work = work.model_copy(update={"deadline": deadline})
            entry = entry.model_copy(
                update={"deadline": deadline, "workload_digest": workload_digest(work)}
            )
            await live.write(entry)
        channel = await live.attach()
        encoded = canonical_bytes(work.model_dump(mode="json"))
        header = SectionedHeader(
            op="accept_workload",
            request_id="delivery",
            sections=(
                SectionDeclaration(
                    name="workload",
                    length=len(encoded),
                    sha256=hashlib.sha256(encoded).hexdigest(),
                ),
            ),
        )
        before = pending_timers(clock)
        try:
            await channel.write_typed_frame(
                FrameType.JSON,
                canonical_bytes(header.model_dump(mode="json")),
                clock.monotonic_ns() + FAR_NS,
            )
            await until(lambda before=before: bool(pending_timers(clock) - before))
            if expire:
                clock.advance(1_000_000_000)
            else:
                await live.write(entry.model_copy(update={"gate_closed": True}))

            await channel.write_typed_frame(
                FrameType.BYTES, encoded, clock.monotonic_ns() + FAR_NS
            )
            _, payload = await channel.read_typed_frame(
                clock.monotonic_ns() + FAR_NS, {FrameType.JSON.value: ENVELOPE_BYTES}
            )
            request_id, error = read_error_envelope(payload)

            assert (request_id, error.code) == ("delivery", "deadline_expired")
        finally:
            channel.close()
