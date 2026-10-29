import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kora.protocols.lifetime import ProcessDomain, ProcessLaunch, ProcessOutput
from kora.protocols.time import SystemClock

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterator
    from typing import BinaryIO

    from kora.runner.identity import GroupObservation, IdentityObservation


def launch(script: str, domain: ProcessDomain) -> ProcessLaunch:
    return ProcessLaunch(
        executable=sys.executable,
        argv=(sys.executable, "-I", "-c", script),
        environment=(),
        working_directory_resource_id="1" * 32,
        stdin=None,
        stdout=ProcessOutput.CAPTURE,
        stderr=ProcessOutput.CAPTURE,
        domain=domain,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", tuple(ProcessDomain))
async def test_gate_requires_acknowledgement_and_retains_wait_anchor(
    tmp_path: Path, domain: ProcessDomain
) -> None:
    from kora.runner.processes import start_gated

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch("open('executed', 'w').close()", domain),
        "2" * 32,
        directory,
        deadline,
        clock,
    )
    os.close(directory)

    try:
        registration = await owner.prepare()
        assert not (tmp_path / "executed").exists()
        assert registration.process_group_id == registration.process.pid
        assert registration.session_id == (
            registration.process.pid
            if domain is ProcessDomain.DETACHED
            else os.getsid(0)
        )
        await owner.acknowledge()
        await owner.wait_exit(deadline)
        await owner.drain(deadline)
        observation = owner.observe()
        before_release = owner.returncode
        status = owner.release()
    finally:
        if owner.returncode is None:
            owner.signal(signal.SIGKILL)
            await owner.wait_exit(deadline)
            await owner.drain(deadline)
            owner.release()

    assert (tmp_path / "executed").exists()
    assert observation.settled
    assert before_release is None
    assert status == 0
    assert owner.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(registration.process.pid, os.WNOHANG)


@pytest.mark.asyncio
async def test_channel_bounds_records_and_reports_peer_close() -> None:
    import asyncio
    import socket

    from kora.runner.channels import (
        MAX_RECORD_BYTES,
        BoundedChannel,
        ChannelError,
        ChannelFailure,
    )

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    first, second = socket.socketpair()
    sender = BoundedChannel(first, clock)
    receiver = BoundedChannel(second, clock)
    value = b"x" * MAX_RECORD_BYTES
    try:
        send = asyncio.create_task(sender.write_record(value, deadline))
        result = await receiver.read_record(deadline, MAX_RECORD_BYTES)
        await send
        with pytest.raises(ChannelError) as capacity:
            await sender.write_record(value + b"x", deadline)
        sender.close()
        with pytest.raises(ChannelError) as closed:
            await receiver.read_frame(deadline)
    finally:
        sender.close()
        receiver.close()

    assert result == value
    assert capacity.value.reason is ChannelFailure.CAPACITY
    assert closed.value.reason is ChannelFailure.CLOSED
    assert sender.closed
    assert receiver.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion", ["data", "busy", "local_close", "peer_close", "cancel"]
)
async def test_idle_channel_owns_and_releases_one_reader(completion: str) -> None:
    import asyncio
    import socket

    from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
    from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock

    first, second = socket.socketpair()
    clock = ControlledLifetimeClock()
    receiver = BoundedChannel(first, clock)
    entered = asyncio.Event()

    async def wait() -> None:
        entered.set()
        await receiver.wait_readable()

    pending = asyncio.create_task(wait())
    try:
        await entered.wait()
        clock.advance(60_000_000_000)
        assert not clock.waiters
        assert not pending.done()
        if completion == "busy":
            with pytest.raises(ChannelError) as busy:
                await receiver.read_frame(clock.monotonic_ns() + 1_000_000_000)
            assert busy.value.reason is ChannelFailure.BUSY
            assert not receiver.closed
        if completion in ("data", "busy"):
            second.sendall((4).to_bytes(4, "big") + b"data")
            await asyncio.wait_for(pending, 5)
            value = await receiver.read_frame(clock.monotonic_ns() + 1_000_000_000)

            assert value == b"data"
            assert not receiver.closed
        elif completion == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            assert receiver.closed
        else:
            if completion == "local_close":
                receiver.close()
                with pytest.raises(ChannelError) as closed:
                    await asyncio.wait_for(pending, 5)
                assert closed.value.reason is ChannelFailure.CLOSED
            else:
                second.close()
                await asyncio.wait_for(pending, 5)
                with pytest.raises(ChannelError) as closed:
                    await receiver.read_frame(clock.monotonic_ns() + 1_000_000_000)
                assert closed.value.reason is ChannelFailure.CLOSED

            assert receiver.closed
    finally:
        receiver.close()
        second.close()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    assert not clock.waiters
    assert pending.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["frame", "record_header", "record_chunk"])
async def test_channel_refuses_length_above_reservation_without_body(
    boundary: str,
) -> None:
    import asyncio
    import socket

    from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
    from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock

    clock = ControlledLifetimeClock()
    first, second = socket.socketpair()
    receiver = BoundedChannel(second, clock)
    prefix = (257).to_bytes(4, "big")
    if boundary == "record_header":
        prefix = (5).to_bytes(4, "big")
    elif boundary == "record_chunk":
        prefix = (
            (4).to_bytes(4, "big") + (1).to_bytes(4, "big") + (2).to_bytes(4, "big")
        )
    first.sendall(prefix)
    deadline = clock.monotonic_ns() + 100_000_000
    receiving = (
        receiver.read_frame(deadline, 256)
        if boundary == "frame"
        else receiver.read_record(deadline, 1)
    )

    try:
        with pytest.raises(ChannelError) as refused:
            await asyncio.wait_for(receiving, 5)
    finally:
        first.close()
        receiver.close()

    assert refused.value.reason is ChannelFailure.CAPACITY
    assert receiver.closed


@pytest.mark.asyncio
async def test_channel_close_releases_a_pending_read_without_waiting_for_deadline() -> (
    None
):
    import asyncio
    import socket

    from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure

    clock = SystemClock()
    first, second = socket.socketpair()
    receiver = BoundedChannel(first, clock)
    entered = asyncio.Event()

    async def read() -> bytes:
        entered.set()
        return await receiver.read_frame(clock.monotonic_ns() + 30_000_000_000)

    pending = asyncio.create_task(read())
    try:
        await entered.wait()
        receiver.close()
        with pytest.raises(ChannelError) as closed:
            await asyncio.wait_for(pending, 1)
    finally:
        pending.cancel()
        second.close()
        receiver.close()

    assert closed.value.reason is ChannelFailure.CLOSED


@pytest.mark.asyncio
async def test_channel_rejects_concurrent_writers_and_releases_cancellation() -> None:
    import asyncio
    import socket

    from kora.runner.channels import (
        MAX_RECORD_BYTES,
        BoundedChannel,
        ChannelError,
        ChannelFailure,
    )

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    first, second = socket.socketpair()
    first.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    sender = BoundedChannel(first, clock)
    receiver = BoundedChannel(second, clock)
    entered = asyncio.Event()

    async def send() -> None:
        entered.set()
        await sender.write_record(b"x" * MAX_RECORD_BYTES, deadline)

    pending = asyncio.create_task(send())
    try:
        await entered.wait()
        with pytest.raises(ChannelError) as busy:
            await sender.write_record(b"other", deadline)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        sender.close()
        with pytest.raises(ChannelError) as closed:
            await receiver.read_record(deadline, MAX_RECORD_BYTES)
    finally:
        pending.cancel()
        sender.close()
        receiver.close()

    assert busy.value.reason is ChannelFailure.BUSY
    assert closed.value.reason is ChannelFailure.CLOSED


@pytest.mark.asyncio
async def test_native_identity_reads_owned_group_parent_identity_and_unknown_pid(
    tmp_path: Path,
) -> None:
    from kora.runner.identity import IdentityState, NativeIdentityReader
    from kora.runner.processes import start_gated

    reader = NativeIdentityReader()
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch("pass", ProcessDomain.INVOCATION),
        "3" * 32,
        directory,
        deadline,
        clock,
    )
    os.close(directory)

    try:
        registration = await owner.prepare()
        observation = reader.read(os.getpid())
        group = reader.group(registration.process_group_id, deadline)
        absent = reader.read(2**31 - 1)
    finally:
        owner.signal(signal.SIGKILL)
        await owner.settle(deadline)
        await owner.drain(deadline)
        owner.release()

    assert observation.state is IdentityState.PRESENT
    process = observation.process
    assert process is not None
    assert process.identity.pid == os.getpid()
    assert process.identity.host_id is not None
    assert process.identity.boot_id is not None
    assert process.identity.pid_namespace is not None
    assert process.process_group_id == os.getpgrp()
    assert process.session_id == os.getsid(0)
    assert not process.dead
    assert group.state is IdentityState.PRESENT
    assert tuple(pid for pid, _ in group.members) == (registration.process.pid,)
    member = group.members[0][1]
    assert member.state is IdentityState.PRESENT
    assert member.process is not None
    assert member.process.identity == registration.process
    assert absent.state is IdentityState.ABSENT


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["eof", "deadline"])
async def test_gate_closure_before_activation_never_executes(
    tmp_path: Path, cause: str
) -> None:
    from kora.runner.channels import ChannelError
    from kora.runner.processes import ProcessError, start_gated

    clock = SystemClock()
    close_deadline = clock.monotonic_ns() + 5_000_000_000
    deadline = (
        clock.monotonic_ns() + 500_000_000 if cause == "deadline" else close_deadline
    )
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch("open('executed', 'w').close()", ProcessDomain.INVOCATION),
        "3" * 32,
        directory,
        deadline,
        clock,
    )
    os.close(directory)
    refused_at: int | None = None
    try:
        if cause == "eof":
            await owner.prepare()
            owner.close_gate()
        else:
            try:
                await owner.prepare()
            except (ChannelError, ProcessError):
                refused_at = clock.monotonic_ns()
        await owner.wait_exit(close_deadline)
        await owner.drain(close_deadline)
        result = owner.release()
    finally:
        if not owner.closed:
            owner.signal(signal.SIGKILL)
            await owner.wait_exit(close_deadline)
            await owner.drain(close_deadline)
            owner.release()

    assert result != 0
    assert not (tmp_path / "executed").exists()
    assert refused_at is None or refused_at >= deadline
    assert owner.closed
    registration = owner.registration
    assert registration is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(registration.process.pid, os.WNOHANG)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["resistant", "descendant"])
async def test_group_anchor_survives_term_and_live_descendant_output(
    tmp_path: Path, mode: str
) -> None:
    import asyncio
    import socket
    import tempfile

    from kora.runner.processes import (
        ProcessCondition,
        ProcessError,
        SignalResult,
        start_gated,
    )

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    script = Path(__file__).resolve().parents[1] / "support/local_lifetime_child.py"
    with tempfile.TemporaryDirectory(prefix="kl-") as endpoint_directory:
        endpoint = str(Path(endpoint_directory) / "s")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(endpoint)
        listener.listen(1)
        listener.setblocking(False)
        request = launch("", ProcessDomain.INVOCATION).model_copy(
            update={"argv": (sys.executable, "-I", str(script), mode, endpoint)}
        )
        directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        source = FilteredIdentityReader()
        owner = start_gated(request, "4" * 32, directory, deadline, clock, source)
        os.close(directory)
        peer: socket.socket | None = None
        try:
            registration = await owner.prepare()
            await owner.acknowledge()
            loop = asyncio.get_running_loop()
            peer, _ = await asyncio.wait_for(loop.sock_accept(listener), 2)
            ready = await asyncio.wait_for(loop.sock_recv(peer, 128), 2)
            assert int(ready) > 0
            if mode == "descendant":
                await owner.wait_exit(deadline)
                source.failure = "omit_once"
                raced = owner.observe()
                assert raced.condition is ProcessCondition.UNKNOWN
            before = owner.observe()
            term = owner.signal(signal.SIGTERM)
            after = owner.observe()
            with pytest.raises(ProcessError):
                owner.release()
            kill = owner.signal(signal.SIGKILL)
            await owner.settle(deadline)
            await owner.drain(deadline)
            result = owner.release()
        finally:
            if not owner.closed:
                owner.signal(signal.SIGKILL)
                await owner.settle(deadline)
                await owner.drain(deadline)
                owner.release()
            if peer is not None:
                peer.close()
            listener.close()

    assert not before.settled
    assert not before.capture_eof
    assert not after.settled
    assert term is SignalResult.SENT
    assert kill is SignalResult.SENT
    assert result == (0 if mode == "descendant" else -signal.SIGKILL)
    assert registration.process_group_id != os.getpgrp()


@pytest.mark.asyncio
async def test_raw_output_is_discarded_without_losing_exact_exit_status(
    tmp_path: Path,
) -> None:
    from kora.runner.processes import start_gated

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    request = launch(
        "import os; data=b'x'*65536; "
        "[(os.write(1,data),os.write(2,data)) for _ in range(64)]; "
        "raise SystemExit(19)",
        ProcessDomain.INVOCATION,
    )
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(request, "5" * 32, directory, deadline, clock)
    os.close(directory)
    try:
        await owner.prepare()
        await owner.acknowledge()
        await owner.wait_exit(deadline)
        await owner.drain(deadline)
        result = owner.release()
    finally:
        if not owner.closed:
            owner.signal(signal.SIGKILL)
            await owner.wait_exit(deadline)
            await owner.drain(deadline)
            owner.release()

    assert result == 19
    assert owner.capture_bytes == 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_failed_exec_keeps_anchor_and_captures_until_release(
    tmp_path: Path,
) -> None:
    from kora.runner.processes import start_gated

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    request = launch("", ProcessDomain.INVOCATION).model_copy(
        update={"executable": str(tmp_path / "absent")}
    )
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(request, "6" * 32, directory, deadline, clock)
    os.close(directory)
    try:
        await owner.prepare()
        await owner.acknowledge()
        await owner.wait_exit(deadline)
        await owner.drain(deadline)
        observed = owner.observe()
        result = owner.release()
    finally:
        if not owner.closed:
            owner.signal(signal.SIGKILL)
            await owner.wait_exit(deadline)
            await owner.drain(deadline)
            owner.release()

    assert observed.settled
    assert result == 72
    assert owner.capture_bytes == 0


@pytest.mark.asyncio
async def test_replacement_preserves_identity_and_excludes_private_descriptors(
    tmp_path: Path,
) -> None:
    from pydantic import TypeAdapter

    from kora.protocols.lifetime import EnvironmentVariable
    from kora.runner.processes import start_gated

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    script = Path(__file__).resolve().parents[1] / "support/local_lifetime_child.py"
    result_path = tmp_path / "inspection.json"
    request = launch("", ProcessDomain.INVOCATION).model_copy(
        update={
            "argv": (sys.executable, "-I", str(script), "inspect", str(result_path)),
            "environment": (EnvironmentVariable(name="EXACT_VALUE", value="private"),),
        }
    )
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    lock = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(lock, True)
    try:
        owner = start_gated(request, "7" * 32, directory, deadline, clock)
    finally:
        os.close(directory)
        os.close(lock)
    try:
        registration = await owner.prepare()
        await owner.acknowledge()
        await owner.settle(deadline)
        await owner.drain(deadline)
        status = owner.release()
    finally:
        if not owner.closed:
            owner.signal(signal.SIGKILL)
            await owner.settle(deadline)
            await owner.drain(deadline)
            owner.release()
    observed = TypeAdapter(dict[str, object]).validate_json(result_path.read_text())

    assert status == 0
    assert observed["pid"] == registration.process.pid
    assert observed["group"] == registration.process_group_id
    assert observed["session"] == registration.session_id
    assert observed["descriptors"] == []
    environment = TypeAdapter(dict[str, str]).validate_python(observed["environment"])
    assert environment["EXACT_VALUE"] == "private"
    assert "KORA_GATE_CONTROL_FD" not in environment
    assert "KORA_GATE_CWD_FD" not in environment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["malformed", "incomplete", "frame", "aggregate", "ack"]
)
async def test_real_gate_rejects_invalid_transport_without_execution(
    tmp_path: Path, failure: str
) -> None:
    import socket
    import subprocess

    from kora.runner.channels import MAX_FRAME_BYTES, MAX_RECORD_BYTES, BoundedChannel
    from kora.runner.gate import ExecutionConfig, encode_deadline
    from kora.runner.identity import IdentityState, NativeIdentityReader
    from kora.runner.processes import ExitAnchor

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    request = launch("open('executed', 'w').close()", ProcessDomain.INVOCATION)
    metadata, payload = ExecutionConfig.from_launch(request).encode()
    parent, child = socket.socketpair()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    parent.sendall(encode_deadline(deadline))
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "kora.runner.gate"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(child.fileno(), directory),
        process_group=0,
        env={
            "KORA_GATE_CONTROL_FD": str(child.fileno()),
            "KORA_GATE_CWD_FD": str(directory),
        },
    )
    anchor = ExitAnchor(process.pid)
    child.close()
    os.close(directory)
    channel = BoundedChannel(parent, clock)
    try:
        if failure == "frame":
            parent.send((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        else:
            await channel.write_frame(
                b"bad" if failure == "malformed" else metadata, deadline
            )
            if failure == "aggregate":
                await channel.write_frame(
                    (MAX_RECORD_BYTES + 1).to_bytes(4, "big"), deadline
                )
            elif failure == "incomplete":
                await channel.write_frame(len(payload).to_bytes(4, "big"), deadline)
                await channel.write_frame(payload[:1], deadline)
            else:
                await channel.write_record(payload, deadline)
                if failure == "ack":
                    assert await channel.read_frame(deadline) == b"ready"
                    await channel.write_frame(b"wrong", deadline)
        channel.close()
        await anchor.wait(deadline, clock)
        group = NativeIdentityReader().group(process.pid)
        assert group.state is IdentityState.PRESENT
        assert tuple(pid for pid, _ in group.members) == (process.pid,)
        pid, status = os.waitpid(process.pid, 0)
        process.returncode = os.waitstatus_to_exitcode(status)
    finally:
        channel.close()
        if process.returncode is None:
            await anchor.wait(deadline, clock)
            _, cleanup_status = os.waitpid(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(cleanup_status)
        anchor.close()

    assert pid == process.pid
    assert process.returncode != 0
    assert not (tmp_path / "executed").exists()


class FilteredIdentityReader:
    def __init__(self) -> None:
        from kora.runner.identity import NativeIdentityReader

        self._native = NativeIdentityReader()
        self.failure: str | None = None

    def read(self, pid: int) -> "IdentityObservation":
        from dataclasses import replace

        from kora.runner.identity import IdentityObservation, IdentityState

        result = self._native.read(pid)
        if self.failure == "unreadable":
            return IdentityObservation(IdentityState.UNKNOWN)
        if self.failure == "reused" and result.process is not None:
            original = result.process
            changed = original.identity.model_copy(
                update={"start_time": original.identity.start_time + 1}
            )
            return replace(result, process=replace(original, identity=changed))
        return result

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> "GroupObservation":
        from dataclasses import replace

        result = self._native.group(process_group_id, deadline_ns)
        if self.failure in ("omit_once", "omit"):
            if self.failure == "omit_once":
                self.failure = None
            return replace(
                result,
                members=tuple(
                    member for member in result.members if member[0] == process_group_id
                ),
            )
        return result


@pytest.mark.asyncio
async def test_known_live_member_omitted_from_both_scans_prevents_release(
    tmp_path: Path,
) -> None:
    import asyncio
    import socket
    import tempfile

    from kora.runner.processes import (
        ExitAnchor,
        ProcessCondition,
        ProcessError,
        start_gated,
    )

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    script = Path(__file__).resolve().parents[1] / "support/local_lifetime_child.py"
    source = FilteredIdentityReader()
    with tempfile.TemporaryDirectory(prefix="kl-") as endpoint_directory:
        endpoint = str(Path(endpoint_directory) / "s")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(endpoint)
        listener.listen(1)
        listener.setblocking(False)
        request = launch("", ProcessDomain.INVOCATION).model_copy(
            update={
                "argv": (sys.executable, "-I", str(script), "descendant", endpoint),
                "stdout": None,
                "stderr": None,
            }
        )
        directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        owner = start_gated(request, "c" * 32, directory, deadline, clock, source)
        os.close(directory)
        peer: socket.socket | None = None
        member_anchor: ExitAnchor | None = None
        try:
            await owner.prepare()
            await owner.acknowledge()
            loop = asyncio.get_running_loop()
            peer, _ = await asyncio.wait_for(loop.sock_accept(listener), 2)
            ready = await asyncio.wait_for(loop.sock_recv(peer, 128), 2)
            member_pid = int(ready)
            member_anchor = ExitAnchor(member_pid, direct_child=False)
            await owner.wait_exit(deadline)
            visible = owner.observe(deadline)
            assert tuple(member.pid for member in visible.live_members) == (member_pid,)

            source.failure = "omit"
            omitted = owner.observe(deadline)
            with pytest.raises(ProcessError) as refused:
                owner.release(deadline)
            retained_returncode = owner.returncode
            retained_owner = not owner.closed
            retained_member = not member_anchor.exited
        finally:
            source.failure = None
            try:
                if peer is not None:
                    await asyncio.get_running_loop().sock_sendall(peer, b"x")
                if member_anchor is not None:
                    await member_anchor.wait(deadline, clock)
                if not owner.closed:
                    owner.signal(signal.SIGKILL, deadline)
                    await owner.settle(deadline)
                    await owner.drain(deadline)
                    owner.release(deadline)
            finally:
                if member_anchor is not None:
                    member_anchor.close()
                if peer is not None:
                    peer.close()
                listener.close()

    assert omitted.condition is ProcessCondition.UNKNOWN
    assert omitted.capture_eof
    assert refused.value.condition is ProcessCondition.UNKNOWN
    assert retained_returncode is None
    assert retained_owner
    assert retained_member


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "reused"])
async def test_unverifiable_identity_refuses_signal_and_reap(
    tmp_path: Path, failure: str
) -> None:
    from kora.runner.identity import IdentityState
    from kora.runner.processes import ProcessError, SignalResult, start_gated

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    source = FilteredIdentityReader()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch("import signal; signal.pause()", ProcessDomain.INVOCATION),
        "8" * 32,
        directory,
        deadline,
        clock,
        source,
    )
    os.close(directory)
    try:
        registration = await owner.prepare()
        await owner.acknowledge()
        source.failure = failure
        refused = owner.signal(signal.SIGKILL)
        with pytest.raises(ProcessError):
            owner.release()
        retained_status = owner.returncode
        source.failure = None
        native = source.read(registration.process.pid)
    finally:
        source.failure = None
        owner.signal(signal.SIGKILL)
        await owner.settle(deadline)
        await owner.drain(deadline)
        owner.release()

    assert refused is (
        SignalResult.UNKNOWN if failure == "unreadable" else SignalResult.MISMATCH
    )
    assert retained_status is None
    assert native.state is IdentityState.PRESENT


@pytest.mark.asyncio
async def test_cancelled_exit_waiter_retains_owner_and_releases_all_descriptors(
    tmp_path: Path,
) -> None:
    import asyncio

    from kora.runner.processes import start_gated

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    baseline = set(os.listdir("/dev/fd"))
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch("import signal; signal.pause()", ProcessDomain.INVOCATION),
        "9" * 32,
        directory,
        deadline,
        clock,
    )
    os.close(directory)
    started = asyncio.Event()

    async def wait_for_exit() -> None:
        started.set()
        await owner.wait_exit(deadline)

    try:
        await owner.prepare()
        await owner.acknowledge()
        waiter = asyncio.create_task(wait_for_exit())
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        retained = owner.returncode
    finally:
        owner.signal(signal.SIGKILL)
        await owner.settle(deadline)
        await owner.drain(deadline)
        owner.release()

    assert retained is None
    assert owner.closed
    assert set(os.listdir("/dev/fd")) - baseline == set()


@pytest.mark.asyncio
async def test_partial_start_failure_closes_parent_descriptors(tmp_path: Path) -> None:
    from kora.runner.processes import ProcessCondition, ProcessError, start_gated

    clock = SystemClock()
    baseline = set(os.listdir("/dev/fd"))

    owner = start_gated(
        launch("open('executed', 'w').close()", ProcessDomain.INVOCATION),
        "a" * 32,
        -1,
        clock.monotonic_ns() + 5_000_000_000,
        clock,
    )
    with pytest.raises(ProcessError) as failed:
        await owner.prepare()

    assert failed.value.condition is ProcessCondition.START_FAILED
    assert owner.closed
    assert set(os.listdir("/dev/fd")) - baseline == set()
    assert not (tmp_path / "executed").exists()


@pytest.mark.asyncio
async def test_explicit_relative_executable_resolves_in_owned_directory(
    tmp_path: Path,
) -> None:
    from kora.runner.processes import start_gated

    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    (tmp_path / "interpreter").symlink_to(sys.executable)
    request = launch(
        "open('executed', 'w').close()", ProcessDomain.INVOCATION
    ).model_copy(update={"executable": "./interpreter"})
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(request, "b" * 32, directory, deadline, clock)
    os.close(directory)
    try:
        await owner.prepare()
        await owner.acknowledge()
        await owner.settle(deadline)
        await owner.drain(deadline)
        result = owner.release()
    finally:
        if not owner.closed:
            owner.signal(signal.SIGKILL)
            await owner.settle(deadline)
            await owner.drain(deadline)
            owner.release()

    assert result == 0
    assert (tmp_path / "executed").exists()


class AdvancingScanClock(SystemClock):
    def __init__(self) -> None:
        self._instant = 0

    def monotonic_ns(self) -> int:
        self._instant += 1_000_000_000
        return self._instant


def test_native_group_scan_refuses_total_entry_capacity_exhaustion() -> None:
    from kora.runner.identity import IdentityState, NativeIdentityReader

    source = NativeIdentityReader(max_scan_entries=1)

    observation = source.group(os.getpgrp())

    assert observation.state is IdentityState.UNKNOWN


def test_native_group_scan_refuses_expired_progress_budget() -> None:
    from kora.runner.identity import IdentityState, NativeIdentityReader

    source = NativeIdentityReader(clock=AdvancingScanClock())

    observation = source.group(os.getpgrp())

    assert observation.state is IdentityState.UNKNOWN


def test_native_group_scan_preserves_callers_absolute_deadline() -> None:
    from kora.runner.identity import IdentityState, NativeIdentityReader

    clock = SystemClock()
    source = NativeIdentityReader(clock=clock)
    expired = clock.monotonic_ns() - 1

    observation = source.group(os.getpgrp(), expired)

    assert observation.state is IdentityState.UNKNOWN


class ReapingStatusOpener:
    def __init__(self, bystander: "subprocess.Popen[bytes]") -> None:
        self.bystander = bystander

    def __call__(self, pid: int) -> "BinaryIO":
        from kora.runner.identity import open_status

        status = open_status(pid)
        if pid == self.bystander.pid:
            self.bystander.kill()
            self.bystander.wait()
        return status


def start_group_leader() -> "subprocess.Popen[bytes]":
    import subprocess

    return subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        process_group=0,
    )


def stop_group_leader(process: "subprocess.Popen[bytes]") -> None:
    if process.returncode is None:
        process.kill()
        process.wait()
    if process.stdin is not None:
        process.stdin.close()


@pytest.fixture
def observed() -> "Iterator[subprocess.Popen[bytes]]":
    process = start_group_leader()
    yield process
    stop_group_leader(process)


@pytest.fixture
def bystander() -> "Iterator[subprocess.Popen[bytes]]":
    process = start_group_leader()
    yield process
    stop_group_leader(process)


def test_native_group_scan_excludes_a_process_reaped_after_its_status_was_opened(
    observed: "subprocess.Popen[bytes]", bystander: "subprocess.Popen[bytes]"
) -> None:
    from kora.runner.identity import IdentityState, NativeIdentityReader

    if sys.platform == "linux":
        source = NativeIdentityReader(status_opener=ReapingStatusOpener(bystander))
        descriptors = len(os.listdir("/proc/self/fd"))

        observation = source.group(observed.pid)

        assert bystander.returncode == -signal.SIGKILL
        assert observation.state is IdentityState.PRESENT
        assert [pid for pid, _ in observation.members] == [observed.pid]
        assert len(os.listdir("/proc/self/fd")) == descriptors
    else:
        source = NativeIdentityReader()
        listed = source.group(observed.pid)
        observed.kill()
        observed.wait()

        observation = source.read(observed.pid)

        assert [pid for pid, _ in listed.members] == [observed.pid]
        assert observation.state is IdentityState.ABSENT


PR_SET_CHILD_SUBREAPER: int = 36


def adopt_orphans(enabled: bool) -> None:
    import ctypes

    control = ctypes.CDLL(None, use_errno=True).prctl
    control.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    control.restype = ctypes.c_int
    if control(PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "child subreaper flag unavailable")


@pytest.mark.asyncio
async def test_gated_process_settles_only_after_descendants_are_reaped(
    tmp_path: Path,
) -> None:
    import asyncio
    import socket
    import tempfile

    from kora.runner.identity import IdentityState, NativeIdentityReader
    from kora.runner.processes import SignalResult, start_gated

    adopting = sys.platform == "linux"
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    script = Path(__file__).resolve().parents[1] / "support/local_lifetime_child.py"
    reader = NativeIdentityReader()
    settled_while_unreaped: bool | None = None
    with tempfile.TemporaryDirectory(prefix="kl-") as endpoint_directory:
        endpoint = str(Path(endpoint_directory) / "s")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer: socket.socket | None = None
        try:
            listener.bind(endpoint)
            listener.listen(1)
            listener.setblocking(False)
            request = launch("", ProcessDomain.INVOCATION).model_copy(
                update={
                    "argv": (sys.executable, "-I", str(script), "descendant", endpoint)
                }
            )
            if adopting:
                adopt_orphans(True)
            directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
            owner = start_gated(request, "6" * 32, directory, deadline, clock)
            os.close(directory)
            descendant: int | None = None
            settling: asyncio.Task[None] | None = None
            reaped = False
            try:
                registration = await owner.prepare()
                await owner.acknowledge()
                loop = asyncio.get_running_loop()
                peer, _ = await asyncio.wait_for(loop.sock_accept(listener), 2)
                descendant = int(await asyncio.wait_for(loop.sock_recv(peer, 128), 2))
                await owner.wait_exit(deadline)
                killed = owner.signal(signal.SIGKILL, deadline)
                settling = asyncio.create_task(owner.settle(deadline))
                if sys.platform == "linux" and killed is SignalResult.SENT:
                    os.waitid(os.P_PID, descendant, os.WEXITED | os.WNOWAIT)
                    await asyncio.wait_for(asyncio.wait(owner.readiness()), 2)
                    settled_while_unreaped = owner.observe(deadline).settled
                    os.waitpid(descendant, 0)
                    reaped = True
                await asyncio.wait_for(settling, 2)
                await owner.drain(deadline)
                owner.release(deadline)
                group = reader.group(registration.process_group_id)
                remaining = reader.read(descendant)
            finally:
                if settling is not None and not settling.done():
                    settling.cancel()
                    await asyncio.gather(settling, return_exceptions=True)
                if not owner.closed:
                    if peer is not None:
                        peer.close()
                        peer = None
                    owner.signal(signal.SIGKILL)
                    await owner.wait_exit(deadline)
                    if adopting and descendant is not None and not reaped:
                        os.waitpid(descendant, 0)
                    await owner.settle(deadline)
                    await owner.drain(deadline)
                    owner.release()
        finally:
            if adopting:
                adopt_orphans(False)
            if peer is not None:
                peer.close()
            listener.close()

    assert killed is SignalResult.SENT
    assert settled_while_unreaped is (False if adopting else None)
    assert group.state is IdentityState.PRESENT
    assert not group.members
    assert remaining.state is IdentityState.ABSENT


HIDDEN_CHILD = """
import ctypes, sys
library = ctypes.CDLL(None, use_errno=True)
if sys.platform == "linux":
    assert library.prctl(4, 0, 0, 0, 0) == 0
else:
    assert library.ptrace(31, 0, None, 0) == 0
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.read()
"""


def test_native_identity_reads_non_dumpable_member_in_observer_namespace() -> None:
    import select
    import subprocess

    from kora.runner.identity import IdentityState, NativeIdentityReader

    reader = NativeIdentityReader()
    own = reader.read(os.getpid())
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", HIDDEN_CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        process_group=0,
    )
    try:
        assert child.stdout is not None
        assert child.stdin is not None
        ready, _, _ = select.select([child.stdout], [], [], 30)
        assert ready
        assert child.stdout.readline() == b"ready\n"
        observation = reader.read(child.pid)
        group = reader.group(child.pid)
    finally:
        child.kill()
        child.wait(timeout=30)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                stream.close()

    assert own.process is not None
    assert observation.state is IdentityState.PRESENT
    assert observation.process is not None
    identity = observation.process.identity
    assert identity.pid == child.pid
    assert identity.pid_namespace == own.process.identity.pid_namespace
    assert identity.leads_group
    assert group.state is IdentityState.PRESENT
    assert [(pid, member.state) for pid, member in group.members] == [
        (child.pid, IdentityState.PRESENT)
    ]
