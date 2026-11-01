import asyncio
import errno
import os
import socket
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.plugins.runtime.local.serving import (
    COMPONENT,
    FIRST_FRAME_NS,
    REVERIFY_NS,
    ControlServer,
)
from kora.plugins.runtime.local.serving import rebound as bind_socket
from kora.protocols.errors import KoraProtocolError
from kora.runner.channels import BoundedChannel, ChannelError
from kora.runner.identity import NativeIdentityReader
from kora.runner.peers import kernel_source
from kora.runner.registry import READY, Running, Submitter
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_peers import (
    AlteredSource,
    GatedBinder,
    GatedSource,
    asked_status,
    client,
    identity_of,
    judging_source,
    served,
    until,
)
from tests.support.local_runners import (
    open_spawning,
    short_directory,
    starting_entry,
    supervised,
    written,
)
from tests.support.local_runtime import runtime_context

CHURN: Final[int] = 20
HELD: Final[int] = 3


def pending(clock: StepClock) -> int:
    return sum(not woken.done() for _, woken in clock.sleepers)


async def ticked(clock: StepClock) -> None:
    await until(lambda: pending(clock) >= 1)
    clock.advance(REVERIFY_NS)
    await until(lambda: pending(clock) >= 1)


async def impostor_churn_is_released(root: Path, capture: Capture) -> None:
    clock = StepClock()
    source = AlteredSource(judging_source(), uid_offset=1)
    with short_directory() as base:
        async with served(base / "sockets", clock, source, capture) as (server, path):
            for _ in range(CHURN):
                channel = await client(path, clock)
                try:
                    with pytest.raises(ChannelError):
                        await asked_status(channel, clock)
                finally:
                    channel.close()
            await until(lambda: server.connections == 0)

    await until(lambda: bool(capture.reader.records()))
    records = [
        record
        for record in capture.reader.records()
        if record.get("event") == "runtime.local.runner.auth_refused"
    ]
    assert [(record.get("reason"), record.get("count")) for record in records] == [
        ("uid", CHURN)
    ]


async def open_connections_are_released_on_close(root: Path, capture: Capture) -> None:
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    with short_directory() as base:
        held: list[BoundedChannel] = []
        try:
            async with served(base / "sockets", clock, source, capture) as (_, path):
                held = [await client(path, clock) for _ in range(HELD)]
                await until(lambda: pending(clock) == HELD + 1)
        finally:
            for channel in held:
                channel.close()


async def rebound(capture: Capture, damage: str) -> tuple[str, os.stat_result | None]:
    clock = StepClock()
    source = kernel_source(NativeIdentityReader())
    squatter = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with short_directory() as base:
            async with served(base / "sockets", clock, source, capture) as (_, path):
                if damage in {"directory", "unsafe-directory"}:
                    (base / "sockets").rename(base / "old-sockets")
                    (base / "sockets").mkdir(mode=0o700)
                    if damage == "unsafe-directory":
                        (base / "sockets").chmod(0o777)
                else:
                    os.unlink(path)
                if damage == "replaced":
                    squatter.bind(path)
                elif damage == "foreign":
                    Path(path).write_bytes(b"")
                await ticked(clock)
                entry = os.stat(path) if os.path.lexists(path) else None
                answered = "none"
                if damage not in {"foreign", "unsafe-directory"}:
                    channel = await client(path, clock)
                    try:
                        value = await asked_status(channel, clock)
                    finally:
                        channel.close()
                    answered = value.status.state
    finally:
        squatter.close()
    return answered, entry


async def removed_socket_is_rebound(root: Path, capture: Capture) -> None:
    answered, entry = await rebound(capture, "removed")

    assert answered == "starting"
    assert entry is not None
    assert stat.S_ISSOCK(entry.st_mode)


async def renamed_directory_is_rebound(root: Path, capture: Capture) -> None:
    answered, entry = await rebound(capture, "directory")

    assert answered == "starting"
    assert entry is not None
    assert stat.S_ISSOCK(entry.st_mode)


async def unsafe_directory_is_refused(root: Path, capture: Capture) -> None:
    answered, entry = await rebound(capture, "unsafe-directory")

    assert answered == "none"
    assert entry is None
    await until(lambda: bool(capture.reader.records()))
    records = [
        record
        for record in capture.reader.records()
        if record.get("event") == "runtime.local.runner.socket_rechecked"
    ]
    assert [record.get("outcome") for record in records] == ["refused"]


async def replaced_socket_is_rebound(root: Path, capture: Capture) -> None:
    answered, entry = await rebound(capture, "replaced")

    assert answered == "starting"
    assert entry is not None
    assert stat.S_ISSOCK(entry.st_mode)


async def foreign_entry_is_left_in_place(root: Path, capture: Capture) -> None:
    answered, entry = await rebound(capture, "foreign")

    assert answered == "none"
    assert entry is not None
    assert stat.S_ISREG(entry.st_mode)


async def status_is_released(root: Path, capture: Capture, close_runtime: bool) -> None:
    ctx = runtime_context(capture)
    clock = StepClock()
    observation_clock = StatusClock()
    own = identity_of(os.getpid())
    source = GatedSource(kernel_source(NativeIdentityReader()))
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "runner-sockets")
        try:
            async with served(base / "sockets", clock, source, capture) as (_, path):
                running = Running(
                    state="running",
                    deadline=None,
                    gate_closed=False,
                    request_digest="sha256:" + "1" * 64,
                    submitter=Submitter(dispatcher_id="d-1", process=own),
                    runner=own,
                    socket_path=path,
                )
                await written(runtime, "attempt-1", running, ctx)
                handle = runtime.registry.key("attempt-1").handle()
                observing = asyncio.ensure_future(
                    runtime.status(handle, replace(ctx, clock=observation_clock))
                )
                await until(source.entered.is_set)
                if close_runtime:
                    admitted = observation_clock.observing
                    assert admitted is not None
                    cancelled_at_close: asyncio.Future[int] = (
                        asyncio.get_running_loop().create_future()
                    )
                    closing = asyncio.create_task(runtime.close())

                    def capture_cancellation() -> None:
                        cancelled_at_close.set_result(admitted.cancelling())

                    asyncio.get_running_loop().call_soon(capture_cancellation)
                    await asyncio.gather(closing, runtime.close())
                    cancellations = await cancelled_at_close
                    assert cancellations > 0
                else:
                    observing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await observing
                source.gate.set()
                if close_runtime:
                    with pytest.raises(KoraProtocolError, match="runtime_closed"):
                        await runtime.status(handle, ctx)
        finally:
            await runtime.close()
    assert source.endpoint is not None
    assert source.endpoint.fileno() == -1


async def cancelled_status_is_released(root: Path, capture: Capture) -> None:
    await status_is_released(root, capture, False)


async def runtime_close_releases_status(root: Path, capture: Capture) -> None:
    await status_is_released(root, capture, True)


async def close_during_rebind(root: Path, capture: Capture, cancel_close: bool) -> None:
    clock = StepClock()
    binder = GatedBinder()
    source = kernel_source(NativeIdentityReader())
    tasks = set(asyncio.all_tasks())
    with short_directory() as base:
        path, name = base / "sockets", "rebound"
        original = bind_socket(path, name)
        server = ControlServer(
            original,
            path,
            name,
            clock,
            source,
            capture.diagnostics.logger(COMPONENT),
            binder=binder,
        )
        server.start()
        closing: asyncio.Task[None] | None = None
        try:
            await until(lambda: pending(clock) == 1)
            os.unlink(path / name)
            clock.advance(REVERIFY_NS)
            await until(binder.entered.is_set)
            fresh = binder.bound
            assert fresh is not None

            closing = asyncio.create_task(server.close())
            await until(lambda: original.listener.fileno() == -1)
            if cancel_close:
                closing.cancel()
                await asyncio.sleep(0)
                closing.cancel()
                await asyncio.sleep(0)

            assert not closing.done()
            assert fresh.listener.fileno() >= 0
            os.fstat(fresh.directory.descriptor)
            binder.gate.set()
            if cancel_close:
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await closing
            assert fresh.listener.fileno() == -1
            assert fresh.directory.closed
            assert original.directory.closed
            with pytest.raises(OSError, match="Bad file descriptor") as fresh_closed:
                os.fstat(fresh.directory.descriptor)
            with pytest.raises(OSError, match="Bad file descriptor") as original_closed:
                os.fstat(original.directory.descriptor)
            assert fresh_closed.value.errno == errno.EBADF
            assert original_closed.value.errno == errno.EBADF
            assert server.connections == 0
            assert pending(clock) == 0
            assert set(asyncio.all_tasks()) == tasks
        finally:
            binder.gate.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await server.close()


async def shutdown_releases_pending_rebind(root: Path, capture: Capture) -> None:
    await close_during_rebind(root, capture, False)


async def cancelled_shutdown_releases_pending_rebind(
    root: Path, capture: Capture
) -> None:
    await close_during_rebind(root, capture, True)


class StatusClock(StepClock):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.observing: asyncio.Task[object] | None = None

    def monotonic_ns(self) -> int:
        if self.observing is None:
            self.observing = asyncio.current_task()
        self.entered.set()
        return super().monotonic_ns()


async def shutdown_at_status_connect(root: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    clock = StatusClock()
    own = identity_of(os.getpid())
    tasks = set(asyncio.all_tasks())
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "runner-sockets")
        try:
            async with served(
                base / "sockets",
                StepClock(),
                kernel_source(NativeIdentityReader()),
                capture,
            ) as (server, path):
                running = Running(
                    state="running",
                    deadline=None,
                    gate_closed=False,
                    request_digest="sha256:" + "1" * 64,
                    submitter=Submitter(dispatcher_id="d-1", process=own),
                    runner=own,
                    socket_path=path,
                )
                await written(runtime, "attempt-1", running, ctx)
                key = runtime.registry.key("attempt-1")
                before = runtime.registry.read(key)

                async def shut_down() -> None:
                    await clock.entered.wait()
                    first = asyncio.create_task(runtime.close())
                    second = asyncio.create_task(runtime.close())
                    asyncio.get_running_loop().call_soon(first.cancel)
                    results = await asyncio.gather(
                        first, second, return_exceptions=True
                    )
                    assert isinstance(results[0], asyncio.CancelledError)
                    assert results[1] is None

                closing = asyncio.create_task(shut_down())
                observing = asyncio.create_task(
                    runtime.status(key.handle(), replace(ctx, clock=clock))
                )
                result = await asyncio.gather(observing, return_exceptions=True)
                await closing

                assert isinstance(result[0], asyncio.CancelledError)
                with pytest.raises(KoraProtocolError, match="runtime_closed"):
                    await runtime.status(key.handle(), ctx)
                await until(lambda: server.connections == 0)
                assert before is not None
        finally:
            await runtime.close()
        reopened = await open_spawning(root, ctx, base / "runner-sockets")
        try:
            assert reopened.registry.read(key) == before
        finally:
            await reopened.close()
    assert set(asyncio.all_tasks()) == tasks


async def cancelled_runtime_close_drains_attestation(
    root: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    own = identity_of(os.getpid())
    source = GatedSource(kernel_source(NativeIdentityReader()))
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "runner-sockets", peers=source)
        try:
            async with served(
                base / "sockets",
                StepClock(),
                kernel_source(NativeIdentityReader()),
                capture,
            ) as (_, path):
                running = Running(
                    state="running",
                    deadline=None,
                    gate_closed=False,
                    request_digest="sha256:" + "1" * 64,
                    submitter=Submitter(dispatcher_id="d-1", process=own),
                    runner=own,
                    socket_path=path,
                )
                await written(runtime, "attempt-1", running, ctx)
                handle = runtime.registry.key("attempt-1").handle()
                observing = asyncio.create_task(runtime.status(handle, ctx))
                await until(source.entered.is_set)

                closing = asyncio.create_task(runtime.close())
                await until(lambda: runtime.executor.closed)
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing

                assert not observing.done()
                assert runtime.executor.jobs
                assert not runtime.registry.directory.closed
                source.gate.set()
                await runtime.close()
                with pytest.raises(asyncio.CancelledError):
                    await observing
                assert not runtime.executor.jobs
                assert runtime.registry.directory.closed
        finally:
            source.gate.set()
            await runtime.close()


async def cancelled_supervisor_releases_resources(root: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    nonce = "1" * 32
    with short_directory() as base:
        sockets = base / "sockets"
        runtime = await open_spawning(root, ctx, sockets)
        try:
            await written(runtime, "attempt-1", starting_entry(sockets, nonce), ctx)
            domain = runtime.execution_domain_id
        finally:
            await runtime.close()
        errors = root.parent / "supervisor-cancellation.stderr"

        result = await supervised(
            (
                sys.executable,
                "-m",
                "tests.plugins.runtime.local.supervisor_leg",
                str(root),
                domain,
                nonce,
            ),
            errors,
        )

        assert result.code == 0, errors.read_text()
        assert result.readiness == READY


class ExpirationCleanupClock(StepClock):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelling = asyncio.Event()
        self.released = asyncio.Event()

    async def sleep_until(self, deadline_ns: int) -> None:
        expiration = deadline_ns == self.monotonic_ns() + FIRST_FRAME_NS
        if expiration:
            self.started.set()
        try:
            await super().sleep_until(deadline_ns)
        except asyncio.CancelledError:
            if expiration:
                self.cancelling.set()
                await self.released.wait()
            raise


async def shutdown_during_expiration_cleanup(root: Path, capture: Capture) -> None:
    clock = ExpirationCleanupClock()
    source = GatedSource(AlteredSource(judging_source(), uid_offset=1))
    with short_directory() as base:
        async with served(base / "sockets", clock, source, capture) as (server, path):
            channel = await client(path, StepClock())
            try:
                await until(clock.started.is_set)
                source.gate.set()
                await until(clock.cancelling.is_set)

                await server.close()

                assert source.endpoint is not None
                assert source.endpoint.fileno() == -1
                assert server.connections == 0
                assert pending(clock) == 0
            finally:
                source.gate.set()
                clock.released.set()
                channel.close()
