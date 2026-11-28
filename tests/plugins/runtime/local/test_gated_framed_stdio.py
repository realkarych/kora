import asyncio
import errno
import os
import signal
import stat
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import pytest

from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.protocols.lifetime import (
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    new_operation_id,
)
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
from kora.runner.processes import (
    GatedProcess,
    ProcessCondition,
    ProcessError,
    SignalResult,
    start_gated,
)
from tests.plugins.runtime.local.frames_support import StepClock, frame
from tests.support.local_runtime import open_descriptors

WINDOW_NS = 10_000_000_000
REQUEST = 4
REPLY = 5
STDERR = b"fixed stderr\n"
ECHO = """
import os
import socket
import struct

with socket.socket(fileno=0) as peer, peer.makefile("rb") as reader:
    length, kind = struct.unpack("!IB", reader.read(5))
    if kind != 4 or length > 64:
        raise SystemExit(3)
    payload = reader.read(length)
    if len(payload) != length:
        raise SystemExit(4)
    result = struct.pack("!Q", os.getpid()) + payload
    peer.sendall(struct.pack("!IB", len(result), 5) + result)
    peer.sendall(struct.pack("!IB", 0, 5))
    os.write(2, b"fixed stderr\\n")
"""
PARTIAL = """
import signal
import socket

with socket.socket(fileno=0) as peer:
    peer.sendall(b"\\x00")
    signal.pause()
"""


def launch(script: str, stdout: ProcessOutput | None = None) -> ProcessLaunch:
    return ProcessLaunch(
        executable=sys.executable,
        argv=(sys.executable, "-I", "-c", script),
        environment=(),
        working_directory_resource_id="a" * 32,
        stdin=None,
        stdout=stdout,
        stderr=ProcessOutput.CAPTURE,
        domain=ProcessDomain.DETACHED,
    )


@asynccontextmanager
async def framed_gate(
    cwd: Path,
    script: str,
    clock: Clock,
    cutoff: int,
    reaper: AdoptedReaper,
    *,
    raw: bool = True,
) -> AsyncGenerator[GatedProcess]:
    descriptor = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY)
    try:
        owner = start_gated(
            launch(script),
            new_operation_id(),
            descriptor,
            cutoff,
            clock,
            raw_stdio=raw,
            stdio_limit=None if raw else 65536,
            wait_owner=reaper,
        )
    finally:
        os.close(descriptor)
    try:
        yield owner
    finally:
        if not owner.closed:
            cleanup = clock.monotonic_ns() + WINDOW_NS
            async with asyncio.timeout(10):
                owner.signal(signal.SIGKILL, cleanup)
                await owner.settle(cleanup)
                await owner.drain(cleanup)
                owner.release(cleanup)


async def echo(channel: BoundedChannel, pid: int, payload: bytes, cutoff: int) -> None:
    await channel.write_typed_frame(REQUEST, payload, cutoff)
    response = await channel.read_typed_frame(cutoff, {REPLY: len(payload) + 8})
    empty = await channel.read_typed_frame(cutoff, {REPLY: 0})
    await channel.expect_eof(cutoff)

    assert response == (REPLY, pid.to_bytes(8, "big") + payload)
    assert empty == (REPLY, b"")
    assert channel.closed


async def collect(owner: GatedProcess, cutoff: int, pid: int, status: int) -> None:
    await owner.settle(cutoff)
    await owner.drain(cutoff)
    assert owner.returncode is None
    assert owner.release(cutoff) == status
    assert owner.release(cutoff) == status
    assert owner.closed
    assert not owner.gate_close_failed
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def open_socket_descriptors() -> set[int]:
    descriptors: set[int] = set()
    for entry in os.listdir("/dev/fd"):
        descriptor = int(entry)
        try:
            mode = os.fstat(descriptor).st_mode
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            if stat.S_ISSOCK(mode):
                descriptors.add(descriptor)
    return descriptors


@pytest.mark.asyncio
async def test_gated_raw_channel_binds_one_borrow_to_the_activated_original(
    tmp_path: Path,
) -> None:
    baseline = open_descriptors()
    clock = SystemClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    reaper = AdoptedReaper(interval=0.01)
    reaper.start()
    try:
        async with (
            framed_gate(tmp_path, ECHO, clock, cutoff, reaper) as sibling,
            framed_gate(tmp_path, ECHO, clock, cutoff, reaper) as owner,
        ):
            other = await sibling.prepare()
            await sibling.acknowledge()
            original = await owner.prepare()
            with pytest.raises(ChannelError) as early:
                owner.borrow_channel(original)
            await owner.acknowledge()
            with pytest.raises(ChannelError) as cross_mode:
                owner.borrow_stdio()
            with pytest.raises(ProcessError) as substitute:
                owner.borrow_channel(other)
            with pytest.raises(ProcessError) as copy:
                owner.borrow_channel(original.model_copy())
            channel = owner.borrow_channel(original)
            with pytest.raises(ChannelError) as repeated:
                owner.borrow_channel(original)
            with pytest.raises(ProcessError):
                owner.release(cutoff)
            await echo(channel, original.process.pid, b"fixed body", cutoff)
            await collect(owner, cutoff, original.process.pid, 0)
            await echo(sibling.borrow_channel(other), other.process.pid, b"", cutoff)
            await collect(sibling, cutoff, other.process.pid, 0)

            assert early.value.reason is ChannelFailure.CLOSED
            assert cross_mode.value.reason is ChannelFailure.CLOSED
            assert substitute.value.condition is ProcessCondition.MISMATCH
            assert copy.value.condition is ProcessCondition.MISMATCH
            assert repeated.value.reason is ChannelFailure.BUSY
            assert owner.capture_bytes == len(STDERR)
            assert sibling.capture_bytes == len(STDERR)
    finally:
        await reaper.stop()
    assert open_descriptors() == baseline


@pytest.mark.asyncio
async def test_gated_raw_channel_uses_inherited_cutoff_for_later_frame_deadline(
    tmp_path: Path,
) -> None:
    baseline = open_descriptors()
    clock = StepClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    first = asyncio.Event()
    reaper = AdoptedReaper()
    async with framed_gate(tmp_path, PARTIAL, clock, cutoff, reaper) as owner:
        original = await owner.prepare()
        await owner.acknowledge()
        channel = owner.borrow_channel(original)

        def received() -> None:
            clock.waiting.clear()
            first.set()

        async with asyncio.timeout(10):
            reader = asyncio.create_task(
                channel.read_typed_frame(
                    cutoff + WINDOW_NS, {REPLY: 1}, received=received
                )
            )
            await first.wait()
            await clock.waiting.wait()
            deadlines = [limit for limit, wait in clock.sleepers if not wait.done()]
            clock.advance(WINDOW_NS)
            with pytest.raises(ChannelError) as expired:
                await reader
            with pytest.raises(ChannelError) as late:
                owner.borrow_channel(original)
            cleanup = clock.monotonic_ns() + WINDOW_NS
            stopped = owner.signal(signal.SIGTERM, cleanup)
            await collect(owner, cleanup, original.process.pid, -signal.SIGTERM)

        assert deadlines == [cutoff]
        assert expired.value.reason is ChannelFailure.DEADLINE
        assert late.value.reason is ChannelFailure.DEADLINE
        assert channel.closed
        assert stopped is SignalResult.SENT
    assert open_descriptors() == baseline


@pytest.mark.asyncio
async def test_gated_stop_revokes_the_active_raw_channel_and_collects_original(
    tmp_path: Path,
) -> None:
    baseline = open_descriptors()
    clock = StepClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    first = asyncio.Event()
    reaper = AdoptedReaper()
    async with framed_gate(tmp_path, PARTIAL, clock, cutoff, reaper) as owner:
        original = await owner.prepare()
        await owner.acknowledge()
        channel = owner.borrow_channel(original)

        def received() -> None:
            clock.waiting.clear()
            first.set()

        async with asyncio.timeout(10):
            reader = asyncio.create_task(
                channel.read_typed_frame(cutoff, {REPLY: 1}, received=received)
            )
            await first.wait()
            await clock.waiting.wait()
            assert not reader.done()
            assert not channel.closed

            stopped = owner.signal(signal.SIGTERM, cutoff)
            closed_at_signal = channel.closed
            with pytest.raises(ChannelError) as revoked:
                await reader
            await collect(owner, cutoff, original.process.pid, -signal.SIGTERM)

        assert stopped is SignalResult.SENT
        assert closed_at_signal
        assert revoked.value.reason is ChannelFailure.CLOSED
        assert not any(not waiter.done() for _, waiter in clock.sleepers)
    assert open_descriptors() == baseline


@pytest.mark.asyncio
async def test_gated_raw_channel_cannot_be_borrowed_after_settlement_before_reap(
    tmp_path: Path,
) -> None:
    baseline = open_descriptors()
    clock = SystemClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    reaper = AdoptedReaper()
    async with framed_gate(tmp_path, "pass", clock, cutoff, reaper) as owner:
        original = await owner.prepare()
        await owner.acknowledge()
        await owner.settle(cutoff)

        assert owner.returncode is None
        with pytest.raises(ChannelError) as refused:
            owner.borrow_channel(original)
        await collect(owner, cutoff, original.process.pid, 0)

        assert refused.value.reason is ChannelFailure.CLOSED
    assert open_descriptors() == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "acknowledge"])
async def test_gated_raw_channel_is_released_after_failed_activation(
    tmp_path: Path, phase: Literal["prepare", "acknowledge"]
) -> None:
    baseline = open_descriptors()
    existing_sockets = open_socket_descriptors()
    clock = StepClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    reaper = AdoptedReaper()
    async with framed_gate(tmp_path, ECHO, clock, cutoff, reaper) as owner:
        original = owner.registration
        assert original is not None
        owned_sockets = open_socket_descriptors() - existing_sockets
        assert len(owned_sockets) == 2
        if phase == "prepare":
            clock.advance(WINDOW_NS)
            with pytest.raises(ChannelError) as expired:
                await owner.prepare()
            assert expired.value.reason is ChannelFailure.DEADLINE
        else:
            with pytest.raises(ProcessError) as unprepared:
                await owner.acknowledge()
            assert unprepared.value.condition is ProcessCondition.UNKNOWN

        assert owner.returncode is None
        for descriptor in owned_sockets:
            with pytest.raises(OSError, match=os.strerror(errno.EBADF)) as closed:
                os.fstat(descriptor)
            assert closed.value.errno == errno.EBADF

        with pytest.raises(ChannelError) as refused:
            owner.borrow_channel(original)
        cleanup = clock.monotonic_ns() + WINDOW_NS
        status = await owner.release_unactivated(cleanup)

        assert refused.value.reason is ChannelFailure.CLOSED
        assert status is not None
        assert status != 0
        assert owner.closed
        assert not owner.gate_close_failed
        with pytest.raises(ChildProcessError):
            os.waitpid(original.process.pid, os.WNOHANG)
    assert open_descriptors() == baseline


@pytest.mark.asyncio
async def test_gated_raw_channel_refuses_conflicting_modes() -> None:
    baseline = open_descriptors()
    clock = SystemClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    with pytest.raises(ProcessError) as stream_conflict:
        start_gated(
            launch(ECHO),
            new_operation_id(),
            -1,
            cutoff,
            clock,
            stdio_limit=1024,
            raw_stdio=True,
        )
    with pytest.raises(ProcessError) as capture_conflict:
        start_gated(
            launch(ECHO, ProcessOutput.CAPTURE),
            new_operation_id(),
            -1,
            cutoff,
            clock,
            raw_stdio=True,
        )

    assert stream_conflict.value.condition is ProcessCondition.START_FAILED
    assert capture_conflict.value.condition is ProcessCondition.START_FAILED
    assert open_descriptors() == baseline


@pytest.mark.asyncio
async def test_gated_stream_mode_remains_available_after_refusing_raw_borrow(
    tmp_path: Path,
) -> None:
    baseline = open_descriptors()
    clock = SystemClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    reaper = AdoptedReaper()
    async with framed_gate(tmp_path, ECHO, clock, cutoff, reaper, raw=False) as owner:
        original = await owner.prepare()
        await owner.acknowledge()
        with pytest.raises(ChannelError) as raw:
            owner.borrow_channel(original)
        reader, writer = owner.borrow_stdio()
        expected = frame(REPLY, original.process.pid.to_bytes(8, "big")) + frame(
            REPLY, b""
        )
        writer.write(frame(REQUEST, b""))
        async with asyncio.timeout(5):
            await writer.drain()
            echoed = await reader.readexactly(len(expected))
            terminal = await reader.read(1)
        await collect(owner, cutoff, original.process.pid, 0)

        assert raw.value.reason is ChannelFailure.CLOSED
        assert echoed == expected
        assert terminal == b""
        assert writer.is_closing()
    assert open_descriptors() == baseline
