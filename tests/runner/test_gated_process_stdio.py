import asyncio
import os
import signal
import socket
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal, cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.bound_checkout import BoundCheckout
from kora.plugins.runtime.local.bound_gate_fence import BoundGateFence
from kora.plugins.runtime.local.bound_slots import BoundRoleSlots
from kora.plugins.runtime.local.environment import (
    CandidateRoleEnvironment,
    bind_candidate_environments,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.lifetime import (
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.process_stdio import GatedStdio
from kora.runner.processes import (
    GatedProcess,
    ProcessCondition,
    ProcessError,
    start_gated,
)

__all__ = ["kora_capture"]

WINDOW_NS = 5_000_000_000
MARK = "open('executed', 'w').close()"
ECHO = (
    f"{MARK}\nimport sys\nsys.stdout.write(sys.stdin.readline())\nsys.stdout.flush()\n"
)


def launch(script: str, stdout: ProcessOutput | None = None) -> ProcessLaunch:
    return ProcessLaunch(
        executable=sys.executable,
        argv=(sys.executable, "-I", "-c", script),
        environment=(),
        working_directory_resource_id="a" * 32,
        stdin=None,
        stdout=stdout,
        stderr=ProcessOutput.CAPTURE,
        domain=ProcessDomain.INVOCATION,
    )


@asynccontextmanager
async def gated(
    directory: Path | DirectoryAnchor, script: str, deadline: int, clock: SystemClock
) -> AsyncGenerator[GatedProcess]:
    with ExitStack() as owned:
        if isinstance(directory, Path):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            owned.callback(os.close, descriptor)
        else:
            descriptor = directory.descriptor
        owner = start_gated(
            launch(script),
            new_operation_id(),
            descriptor,
            deadline,
            clock,
            stdio_limit=131_072,
        )
    try:
        yield owner
    finally:
        if not owner.closed:
            cutoff = clock.monotonic_ns() + WINDOW_NS
            owner.close_gate()
            owner.signal(signal.SIGKILL, cutoff)
            await owner.settle(cutoff)
            await owner.drain(cutoff)
            owner.release(cutoff)


class SetupClock(SystemClock):
    def __init__(self) -> None:
        self.on_wait: Callable[[], None] | None = None
        self._offset = 0

    def monotonic_ns(self) -> int:
        return super().monotonic_ns() + self._offset

    def expire(self, deadline: int) -> None:
        self._offset = max(0, deadline - super().monotonic_ns())

    async def sleep_until(self, deadline_ns: int) -> None:
        on_wait = self.on_wait
        self.on_wait = None
        if on_wait is not None:
            on_wait()
        await super().sleep_until(deadline_ns)


@pytest.mark.asyncio
async def test_bound_checkout_descriptor_reaches_real_gate_after_path_move(
    tmp_path: Path,
) -> None:
    workspace = tmp_path.resolve() / "workspace"
    moved = workspace.with_name("moved")
    workspace.mkdir(mode=0o700)
    clock = SystemClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    checkout = BoundCheckout.capture(workspace)
    try:
        checkout.verify(workspace, cutoff, clock)
        workspace.rename(moved)
        workspace.mkdir(mode=0o700)

        async with gated(checkout.anchor, MARK, cutoff, clock) as owner:
            await owner.prepare()
            await owner.acknowledge()
            await owner.settle(cutoff)
            await owner.drain(cutoff)
            result = owner.release(cutoff)
    finally:
        checkout.close()

    assert result == 0
    assert owner.closed
    assert checkout.anchor.closed
    assert (moved / "executed").is_file()
    assert not (workspace / "executed").exists()


@pytest.mark.asyncio
async def test_gated_stdio_is_borrowed_once_after_activation(tmp_path: Path) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + WINDOW_NS
    marker = tmp_path / "executed"
    child = "import os\nprint(os.getpid(), flush=True)\n" + ECHO
    async with gated(tmp_path, child, deadline, clock) as owner:
        start = asyncio.Event()

        async def borrow() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            await start.wait()
            return owner.borrow_stdio()

        registration = await owner.prepare()
        before_activation = marker.exists()
        with pytest.raises(ChannelError) as early:
            owner.borrow_stdio()
        await owner.acknowledge()
        start.set()
        streams, duplicate = await asyncio.gather(
            borrow(), borrow(), return_exceptions=True
        )
        assert isinstance(streams, tuple)
        reader, writer = streams
        writer.write(b"owned stream\n")
        async with asyncio.timeout(5):
            await writer.drain()
            responder = await reader.readline()
            echoed = await reader.readline()
            eof = await reader.read(1)
        with pytest.raises(ProcessError) as early_release:
            owner.release(deadline)
        before_reap = owner.returncode
        await owner.settle(deadline)
        await owner.drain(deadline)
        observed = owner.observe(deadline)
        result = owner.release(deadline)
        with pytest.raises(ChannelError) as fenced:
            owner.borrow_stdio()

        assert not before_activation
        assert early.value.reason is ChannelFailure.CLOSED
        assert isinstance(duplicate, ChannelError)
        assert duplicate.reason is ChannelFailure.BUSY
        assert responder == f"{registration.process.pid}\n".encode()
        assert echoed == b"owned stream\n"
        assert eof == b""
        assert marker.is_file()
        assert observed.settled
        assert observed.capture_eof
        assert early_release.value.condition is ProcessCondition.UNKNOWN
        assert before_reap is None
        assert result == 0
        assert writer.is_closing()
        assert fenced.value.reason is ChannelFailure.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["stop", "cancel", "deadline"])
async def test_gated_stdio_setup_interruption_refuses_delivery(
    tmp_path: Path, interruption: Literal["stop", "cancel", "deadline"]
) -> None:
    clock = SetupClock()
    deadline = clock.monotonic_ns() + WINDOW_NS
    observed = asyncio.Event()
    async with gated(tmp_path, MARK, deadline, clock) as owner:
        preparing = asyncio.create_task(owner.prepare())

        def interrupt() -> None:
            assert not preparing.done()
            observed.set()
            match interruption:
                case "stop":
                    owner.close_gate()
                case "cancel":
                    preparing.cancel()
                case "deadline":
                    clock.expire(deadline)

        clock.on_wait = interrupt

        (outcome,) = await asyncio.gather(preparing, return_exceptions=True)
        cutoff = clock.monotonic_ns() + WINDOW_NS
        await owner.settle(cutoff)
        await owner.drain(cutoff)
        status = owner.release(cutoff)
        with pytest.raises(ChannelError) as fenced:
            owner.borrow_stdio()

        assert observed.is_set()
        if interruption == "cancel":
            assert isinstance(outcome, asyncio.CancelledError)
        else:
            assert isinstance(outcome, ChannelError)
            assert outcome.reason is (
                ChannelFailure.DEADLINE
                if interruption == "deadline"
                else ChannelFailure.CLOSED
            )
        assert fenced.value.reason is ChannelFailure.CLOSED
        assert status != 0
        assert not (tmp_path / "executed").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [False, True])
async def test_gated_stdio_failed_acknowledgement_refuses_execution(
    tmp_path: Path,
    prepared: bool,
) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + WINDOW_NS
    async with gated(tmp_path, MARK, deadline, clock) as owner:
        if prepared:
            await owner.prepare()
            owner.close_gate()

        with pytest.raises(ProcessError) as refused:
            await owner.acknowledge()
        await owner.settle(deadline)
        await owner.drain(deadline)
        status = owner.release(deadline)

        assert refused.value.condition is ProcessCondition.UNKNOWN
        assert status != 0
        assert not (tmp_path / "executed").exists()


@pytest.mark.asyncio
async def test_one_bound_stop_fences_both_prepared_gates(
    tmp_path: Path, kora_capture: Capture
) -> None:
    app_dir, exec_dir = tmp_path / "app", tmp_path / "exec"
    app_dir.mkdir()
    exec_dir.mkdir()
    clock = SystemClock()
    cutoff = clock.monotonic_ns() + WINDOW_NS
    binding = bind_candidate_environments(
        cwd=tmp_path,
        attempt_token=kora_capture.scope.admit("synthetic-attempt"),
        workload={},
        secrets={},
        harness_credentials={},
        roles={role: CandidateRoleEnvironment({}, ()) for role in ("app", "exec")},
    )
    slots = BoundRoleSlots(binding, clock, cutoff)
    fence = BoundGateFence(slots)
    async with (
        gated(exec_dir, MARK, cutoff, clock) as executor,
        gated(app_dir, MARK, cutoff, clock) as app,
    ):
        for owner in (executor, app):
            fence.register(owner)
            await owner.prepare()

        fence.stop()
        with pytest.raises(KoraProtocolError):
            slots.claim("app")
        for owner in fence.gates:
            with pytest.raises(ProcessError) as refused:
                await owner.acknowledge()
            assert refused.value.condition is ProcessCondition.UNKNOWN
        for owner in fence.gates:
            await owner.settle(cutoff)
            await owner.drain(cutoff)
            owner.release(cutoff)

        assert fence.gates == (executor, app)
        assert all(owner.closed and owner.returncode == 72 for owner in fence.gates)
        assert not fence.close_failed
        assert not (exec_dir / "executed").exists()
        assert not (app_dir / "executed").exists()


@pytest.mark.asyncio
async def test_gated_stdio_settlement_deadline_wins_over_transport_completion() -> None:
    clock = SetupClock()
    parent, child = socket.socketpair()
    stdio = GatedStdio(parent, 131_072)
    settled_after_cutoff = asyncio.Event()
    with child:
        try:
            await stdio.prepare(clock.monotonic_ns() + WINDOW_NS, clock)
            cutoff = clock.monotonic_ns() + WINDOW_NS

            def expire() -> None:
                clock.expire(cutoff)
                settled_after_cutoff.set()

            clock.on_wait = expire

            with pytest.raises(ChannelError) as expired:
                await stdio.settle(cutoff, clock)
        finally:
            await stdio.settle(clock.monotonic_ns() + WINDOW_NS, clock)

    assert settled_after_cutoff.is_set()
    assert expired.value.reason is ChannelFailure.DEADLINE
    assert parent.fileno() == -1


@pytest.mark.asyncio
async def test_gated_stdio_cancels_a_borrowed_read_and_settles(tmp_path: Path) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + WINDOW_NS
    async with gated(tmp_path, ECHO, deadline, clock) as owner:
        await owner.prepare()
        await owner.acknowledge()
        reader, writer = owner.borrow_stdio()
        reading = asyncio.Event()

        async def read() -> bytes:
            reading.set()
            return await reader.read(1)

        pending = asyncio.create_task(read())
        await reading.wait()

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        owner.close_gate()
        await owner.settle(deadline)
        await owner.drain(deadline)
        owner.release(deadline)

        assert writer.is_closing()
        assert reader.at_eof()
        assert owner.closed


@pytest.mark.asyncio
async def test_gated_stdio_stop_aborts_backpressured_output(tmp_path: Path) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + WINDOW_NS
    child = "import signal\nprint('ready', flush=True)\nsignal.pause()"
    async with gated(tmp_path, child, deadline, clock) as owner:
        await owner.prepare()
        await owner.acknowledge()
        reader, writer = owner.borrow_stdio()
        async with asyncio.timeout(5):
            ready = await reader.readline()
        endpoint = cast(socket.socket, writer.get_extra_info("socket"))
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        writer.transport.set_write_buffer_limits(high=0)
        entered = asyncio.Event()

        async def flush() -> None:
            entered.set()
            await writer.drain()

        writer.write(b"x" * 131_072)
        pending = asyncio.create_task(flush())
        await entered.wait()
        backpressured = (
            not pending.done() and writer.transport.get_write_buffer_size() > 0
        )
        owner.close_gate()
        owner.signal(signal.SIGTERM, deadline)
        async with asyncio.timeout(5):
            await asyncio.gather(pending, return_exceptions=True)
        await owner.settle(deadline)
        await owner.drain(deadline)
        owner.release(deadline)

        assert ready == b"ready\n"
        assert backpressured
        assert endpoint.fileno() == -1
        assert owner.closed


@pytest.mark.asyncio
async def test_gated_stdio_partial_start_closes_descriptors() -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + WINDOW_NS
    baseline = set(os.listdir("/dev/fd"))

    owner = start_gated(
        launch(MARK), new_operation_id(), -1, deadline, clock, stdio_limit=131_072
    )
    with pytest.raises(ProcessError) as unacquired:
        owner.require_acquired()
    with pytest.raises(ProcessError) as refused:
        await owner.prepare()
    with pytest.raises(ProcessError) as conflicting_capture:
        start_gated(
            launch(MARK, ProcessOutput.CAPTURE),
            new_operation_id(),
            -1,
            deadline,
            clock,
            stdio_limit=131_072,
        )

    assert unacquired.value.condition is ProcessCondition.START_FAILED
    assert refused.value.condition is ProcessCondition.START_FAILED
    assert conflicting_capture.value.condition is ProcessCondition.START_FAILED
    assert owner.closed
    assert set(os.listdir("/dev/fd")) == baseline
