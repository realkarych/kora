import asyncio
import errno
import fcntl
import os
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    FrameType,
    control_request,
    read_status,
)
from kora.plugins.runtime.local.serving import FIRST_FRAME_NS, REVERIFY_NS
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import RuntimeStatus
from kora.runner.channels import ChannelError
from kora.runner.identity import NativeIdentityReader
from kora.runner.peers import CredentialSource, PeerSource, exited, kernel_source
from kora.runner.registry import Running
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_peers import (
    CONDITION_SECONDS,
    FAR_NS,
    SLEEPER,
    AlteredSource,
    GatedSource,
    asked_status,
    client,
    identity_of,
    judging_source,
    listener_refusals,
    refusals,
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
    written,
    zombie_children,
)
from tests.support.local_runtime import (
    recorded,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]

STALE_STAMP_NS: Final[int] = 10**18


def pending(clock: StepClock) -> int:
    return sum(not woken.done() for _, woken in clock.sleepers)


async def refused_by_server(path: str, clock: StepClock) -> None:
    channel = await client(path, clock)
    try:
        with pytest.raises(ChannelError):
            await asked_status(channel, clock)
    finally:
        channel.close()


async def answered(
    sockets: Path, capture: Capture, source: PeerSource
) -> tuple[RuntimeStatus, bool]:
    clock = StepClock()
    async with served(sockets, clock, source, capture) as (_, path):
        channel = await client(path, clock)
        try:
            value = await asked_status(channel, clock)
            await channel.expect_eof(clock.monotonic_ns() + FAR_NS)
        finally:
            channel.close()
    assert value.status.observed_at == clock.now()
    return value.status, value.holds_workload


async def status_is_answered_before_attach(
    root: Path, sockets: Path, capture: Capture
) -> None:
    status, holds = await answered(
        sockets, capture, kernel_source(NativeIdentityReader())
    )

    assert (status.state, status.phase, status.exit_code) == (
        "starting",
        "preparation",
        None,
    )
    assert holds is False
    assert refusals(capture) == {}


async def credential_source_answers_on_a_pidfd_kernel(
    root: Path, sockets: Path, capture: Capture
) -> None:
    status, _ = await answered(
        sockets, capture, CredentialSource(NativeIdentityReader())
    )

    assert status.state == "starting"
    assert refusals(capture) == {}


async def closed_after_first_frame(
    sockets: Path, capture: Capture, frame: tuple[FrameType, bytes]
) -> None:
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    async with served(sockets, clock, source, capture) as (_, path):
        channel = await client(path, clock)
        try:
            await channel.write_typed_frame(*frame, clock.monotonic_ns() + FAR_NS)
            closure_errno = None
            try:
                await channel.expect_eof(clock.monotonic_ns() + FAR_NS)
            except ConnectionResetError as error:
                closure_errno = error.errno
            assert closure_errno in {None, errno.ECONNRESET}
        finally:
            channel.close()


async def other_operation_before_attach_is_closed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    attach = (FrameType.JSON, control_request("heartbeat", "request-1"))

    await closed_after_first_frame(sockets, capture, attach)

    assert refusals(capture) == {"before_attach": 1}


async def bytes_frame_before_attach_is_closed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    await closed_after_first_frame(sockets, capture, (FrameType.BYTES, b"x"))

    assert refusals(capture) == {"malformed": 1}


async def stalled_first_frame_is_closed_after_five_seconds(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    async with served(sockets, clock, source, capture) as (server, path):
        channel = await client(path, clock)
        try:
            await until(lambda: pending(clock) == 2)
            clock.advance(FIRST_FRAME_NS - 1)
            assert server.connections == 1
            clock.advance(1)
            await until(lambda: server.connections == 0)
        finally:
            channel.close()

    assert refusals(capture) == {"handshake_timeout": 1}


async def ninth_unattached_connection_is_closed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    async with served(sockets, clock, source, capture) as (server, path):
        held = [await client(path, clock) for _ in range(8)]
        try:
            await until(lambda: pending(clock) == 17)
            ninth = await client(path, clock)
            await ninth.expect_eof(clock.monotonic_ns() + FAR_NS)
            held[0].close()
            await until(lambda: server.connections == 7)
            tenth = await client(path, clock)
            try:
                value = await asked_status(tenth, clock)
            finally:
                tenth.close()
        finally:
            for channel in held:
                channel.close()

    assert value.status.state == "starting"
    assert refusals(capture) == {}


async def refused_peer(
    sockets: Path, capture: Capture, source: PeerSource
) -> dict[str, int]:
    clock = StepClock()
    async with served(sockets, clock, source, capture) as (_, path):
        await refused_by_server(path, clock)
    return refusals(capture)


async def peer_with_another_uid_is_refused(
    root: Path, sockets: Path, capture: Capture
) -> None:
    source = AlteredSource(judging_source(), uid_offset=1)

    assert await refused_peer(sockets, capture, source) == {"uid": 1}


async def peer_pid_zero_is_refused(root: Path, sockets: Path, capture: Capture) -> None:
    source = AlteredSource(judging_source(), pid=lambda _: 0)

    assert await refused_peer(sockets, capture, source) == {"pid_zero": 1}


async def peer_started_after_accept_is_refused(
    root: Path, sockets: Path, capture: Capture
) -> None:
    source = AlteredSource(judging_source(), stamp_offset=-STALE_STAMP_NS)

    counted = await refused_peer(sockets, capture, source)

    assert counted == {"started_after_accept": 1}


async def reused_peer_pid_is_refused(
    root: Path, sockets: Path, capture: Capture
) -> None:
    started: list[subprocess.Popen[bytes]] = []

    def successor(_: int) -> int:
        process = subprocess.Popen(SLEEPER)
        started.append(process)
        return process.pid

    source = AlteredSource(judging_source(), pid=successor)
    try:
        counted = await refused_peer(sockets, capture, source)
    finally:
        for process in started:
            process.kill()
            process.wait(timeout=CONDITION_SECONDS)

    assert len(started) == 1
    assert counted == {"started_after_accept": 1}


async def peer_exiting_before_attestation_is_refused(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    source = GatedSource(kernel_source(NativeIdentityReader()))
    async with served(sockets, clock, source, capture) as (server, path):
        program = (
            "import socket, sys\n"
            "peer = socket.socket(socket.AF_UNIX)\n"
            "peer.connect(sys.argv[1])\n"
        )
        peer = subprocess.Popen([sys.executable, "-c", program, path])
        await asyncio.to_thread(peer.wait, CONDITION_SECONDS)
        await until(source.entered.is_set)
        source.gate.set()
        await until(lambda: server.connections == 0)

    counted = refusals(capture)
    assert sum(counted.values()) == 1
    assert set(counted) <= {"unreadable", "exited"}


async def observed_status(
    root: Path,
    sockets: Path,
    capture: Capture,
    forge: Callable[[Running], Awaitable[Running]],
) -> RuntimeStatus | KoraProtocolError:
    ctx = runtime_context(capture)
    runtime = await open_spawning(root, ctx, sockets)
    try:
        handle = await runtime.submit(submitted(runtime, "attempt-1"), ctx)
        entry = await recorded(runtime, "attempt-1")
        assert isinstance(entry, Running)
        forged = await forge(entry)
        if forged != entry:
            await written(runtime, "attempt-1", forged, ctx)
        try:
            return await runtime.status(handle, ctx)
        except KoraProtocolError as refused:
            return refused
    finally:
        await runtime.close()


async def unchanged(entry: Running) -> Running:
    return entry


async def running_runner_reports_its_status(
    root: Path, sockets: Path, capture: Capture
) -> None:
    status = await observed_status(root, sockets, capture, unchanged)

    assert isinstance(status, RuntimeStatus)
    assert (status.state, status.phase, status.exit_code) == (
        "starting",
        "preparation",
        None,
    )
    assert listener_refusals(capture) == []


def with_runner(entry: Running, runner: ProcessIdentity) -> Running:
    return entry.model_copy(update={"runner": runner})


async def listener_refused(
    root: Path,
    sockets: Path,
    capture: Capture,
    forge: Callable[[Running], Awaitable[Running]],
) -> None:
    refused = await observed_status(root, sockets, capture, forge)

    assert isinstance(refused, KoraProtocolError)
    assert (refused.code, refused.retryable) == ("runner_unreachable", True)
    assert listener_refusals(capture) == ["not_runner"]


async def another_process_listening_is_refused(
    root: Path, sockets: Path, capture: Capture
) -> None:
    impostor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    async def squatted(entry: Running) -> Running:
        os.unlink(entry.socket_path)
        impostor.bind(entry.socket_path)
        impostor.listen(4)
        return entry

    try:
        await listener_refused(root, sockets, capture, squatted)
    finally:
        impostor.close()


async def forged_runner_is_refused(root: Path, sockets: Path, capture: Capture) -> None:
    with sleeping_process() as other:

        async def forged(entry: Running) -> Running:
            return with_runner(entry, identity_of(other.pid))

        await listener_refused(root, sockets, capture, forged)


async def reused_runner_pid_is_refused(
    root: Path, sockets: Path, capture: Capture
) -> None:
    async def reused(entry: Running) -> Running:
        earlier = entry.runner.start_time - 1
        return with_runner(
            entry, entry.runner.model_copy(update={"start_time": earlier})
        )

    await listener_refused(root, sockets, capture, reused)


async def rebinding_is_logged_without_paths(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    async with served(sockets, clock, source, capture) as (_, path):
        os.unlink(path)
        await until(lambda: pending(clock) == 1)
        clock.advance(REVERIFY_NS)
        await until(lambda: pending(clock) == 1)

    records = [
        record
        for record in capture.finish()
        if record.get("event") == "runtime.local.runner.socket_rechecked"
    ]
    assert [(record.get("level"), record.get("outcome")) for record in records] == [
        ("WARNING", "rebound")
    ]
    assert str(sockets) not in str(records)


async def stalled_attestation_closes_at_deadline(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    source = GatedSource(kernel_source(NativeIdentityReader()))
    async with served(sockets, clock, source, capture) as (_, path):
        channel = await client(path, clock)
        try:
            await until(source.entered.is_set)

            clock.advance(FIRST_FRAME_NS)
            with pytest.raises(ChannelError):
                await asyncio.wait_for(asked_status(channel, clock), CONDITION_SECONDS)

            assert not source.gate.is_set()
        finally:
            source.gate.set()
            channel.close()


async def cancelled_close_drains_attestation(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    source = GatedSource(kernel_source(NativeIdentityReader()))
    async with served(sockets, clock, source, capture) as (server, path):
        channel = await client(path, clock)
        closing: asyncio.Task[None] | None = None
        try:
            await until(source.entered.is_set)
            closing = asyncio.create_task(server.close())
            await until(lambda: pending(clock) == 1)

            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)

            assert not closing.done()
            assert server.connections == 1
            source.gate.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, CONDITION_SECONDS)
            assert server.connections == 0
        finally:
            source.gate.set()
            channel.close()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)


async def nonreading_status_peer_releases_slot(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    source = GatedSource(kernel_source(NativeIdentityReader()))
    async with served(sockets, clock, source, capture) as (server, path):
        channel = await client(path, clock)
        try:
            await until(source.entered.is_set)
            request = control_request("status", "unread-request")

            await channel.write_typed_frame(
                FrameType.JSON, request, clock.monotonic_ns() + FAR_NS
            )
            source.gate.set()
            await until(lambda: server.connections == 0)

            assert pending(clock) == 1
            _, payload = await channel.read_typed_frame(
                clock.monotonic_ns() + FAR_NS, {FrameType.JSON.value: ENVELOPE_BYTES}
            )
            assert read_status(payload, "unread-request").status.state == "starting"
            await channel.expect_eof(clock.monotonic_ns() + FAR_NS)
        finally:
            source.gate.set()
            channel.close()


LEGS: dict[str, Callable[[Path, Path, Capture], Awaitable[None]]] = {
    "nonreading_status_peer_releases_slot": nonreading_status_peer_releases_slot,
    "cancelled_close_drains_attestation": cancelled_close_drains_attestation,
    "rebinding_is_logged_without_paths": rebinding_is_logged_without_paths,
    "status_is_answered_before_attach": status_is_answered_before_attach,
    "stalled_attestation_closes_at_deadline": stalled_attestation_closes_at_deadline,
    "other_operation_before_attach_is_closed": (
        other_operation_before_attach_is_closed
    ),
    "bytes_frame_before_attach_is_closed": bytes_frame_before_attach_is_closed,
    "stalled_first_frame_is_closed_after_five_seconds": (
        stalled_first_frame_is_closed_after_five_seconds
    ),
    "ninth_unattached_connection_is_closed": ninth_unattached_connection_is_closed,
    "peer_with_another_uid_is_refused": peer_with_another_uid_is_refused,
    "peer_pid_zero_is_refused": peer_pid_zero_is_refused,
    "peer_started_after_accept_is_refused": peer_started_after_accept_is_refused,
    "reused_peer_pid_is_refused": reused_peer_pid_is_refused,
    "peer_exiting_before_attestation_is_refused": (
        peer_exiting_before_attestation_is_refused
    ),
    "running_runner_reports_its_status": running_runner_reports_its_status,
    "another_process_listening_is_refused": another_process_listening_is_refused,
    "forged_runner_is_refused": forged_runner_is_refused,
    "reused_runner_pid_is_refused": reused_runner_pid_is_refused,
}


async def high_pidfd_tracks_process_exit(
    root: Path, sockets: Path, capture: Capture
) -> None:
    if sys.platform != "linux":
        raise AssertionError("pidfd requires Linux")
    with sleeping_process() as process:
        descriptor = os.pidfd_open(process.pid)
        try:
            high_descriptor = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 2048)
            try:
                before_exit = exited(high_descriptor)

                process.kill()
                process.wait(timeout=CONDITION_SECONDS)
                after_exit = exited(high_descriptor)

                assert before_exit is False
                assert after_exit is True
            finally:
                os.close(high_descriptor)
        finally:
            os.close(descriptor)


LINUX_LEGS: dict[str, Callable[[Path, Path, Capture], Awaitable[None]]] = {
    "high_pidfd_tracks_process_exit": high_pidfd_tracks_process_exit,
    "credential_source_answers_on_a_pidfd_kernel": (
        credential_source_answers_on_a_pidfd_kernel
    ),
}
PLATFORM_LEGS: dict[str, Callable[[Path, Path, Capture], Awaitable[None]]] = {
    **LEGS,
    **(LINUX_LEGS if sys.platform == "linux" else {}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", sorted(PLATFORM_LEGS))
async def test_control_channel_rejects_impostors(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    root = state_root(tmp_path)
    zombies = zombie_children()
    with short_directory() as base:
        sockets = base / "sockets"
        try:
            await PLATFORM_LEGS[leg](root, sockets, kora_capture)
        finally:
            reap_runners(root)

    assert runner_pids(root) == ()
    assert zombie_children() == zombies
