import asyncio
import errno
import os
import sys
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, chdir
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import COMPONENT, REVERIFY_NS, ControlServer
from kora.plugins.runtime.local.supervisor import (
    PreparedRegistration,
    RunnerArguments,
    RunnerRefused,
    prepare_registration,
)
from kora.runner.identity import NativeIdentityReader
from kora.runner.registry import Fence, Running, Starting
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_peers import asked_status, client, until
from tests.support.local_runners import (
    open_spawning,
    own_identity,
    short_directory,
    starting_entry,
    written,
)
from tests.support.local_runtime import (
    open_descriptors,
    runtime_context,
    state_root,
    stopped_holder,
)

__all__ = ["kora_capture"]
NONCE: Final[str] = "c" * 32


@asynccontextmanager
async def starting_registration(
    root: Path, capture: Capture
) -> AsyncGenerator[tuple[LocalRuntime, RunnerArguments]]:
    ctx = runtime_context(capture)
    with short_directory() as base:
        sockets = base / "sockets"
        runtime = await open_spawning(root, ctx, sockets)
        try:
            await written(runtime, "attempt-1", starting_entry(sockets, NONCE), ctx)
            yield (
                runtime,
                RunnerArguments(root, "attempt-1", runtime.execution_domain_id, NONCE),
            )
        finally:
            await runtime.close()


def prepared_at(arguments: RunnerArguments) -> PreparedRegistration:
    with chdir(Path.cwd()):
        return prepare_registration(arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["domain", "nonce", "fence", "collision"])
async def test_runner_preparation_releases_partial_resources(
    tmp_path: Path, kora_capture: Capture, failure: str
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        runtime,
        arguments,
    ):
        ctx = runtime_context(kora_capture)
        entry = runtime.registry.entry(runtime.registry.key(arguments.attempt_id))
        assert isinstance(entry, Starting)
        path = Path(entry.socket_directory)
        collision = path / runtime.registry.key(arguments.attempt_id).name[:16]
        if failure == "domain":
            arguments = replace(arguments, execution_domain_id="other-domain")
        elif failure == "nonce":
            arguments = replace(arguments, nonce="d" * 32)
        elif failure == "fence":
            fence = Fence(retirement_id="retire-1", fenced_at=ctx.clock.now())
            await written(
                runtime, arguments.attempt_id, starting_entry(path, NONCE, fence), ctx
            )
        else:
            collision.mkdir()
        before = open_descriptors()
        expected = OSError if failure == "collision" else RunnerRefused

        with pytest.raises(expected) as refused:
            prepared_at(arguments)

        assert open_descriptors() == before
        if failure == "collision":
            assert isinstance(refused.value, OSError)
            expected_errno = errno.EPERM if sys.platform == "darwin" else errno.EISDIR
            assert refused.value.errno == expected_errno
            assert collision.is_dir()


@pytest.mark.asyncio
async def test_runner_preparation_registration_refusal_releases_socket(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        runtime,
        arguments,
    ):
        before = open_descriptors()
        prepared = prepared_at(arguments)
        ctx = runtime_context(kora_capture)
        fence = Fence(retirement_id="retire-1", fenced_at=ctx.clock.now())
        await written(
            runtime,
            arguments.attempt_id,
            starting_entry(Path(prepared.entry.socket_directory), NONCE, fence),
            ctx,
        )
        path = Path(prepared.path)
        bound = prepared.bound
        assert bound is not None

        try:
            with pytest.raises(RunnerRefused, match="fenced"):
                await prepared.register(own_identity(), ctx.clock)
        finally:
            prepared.close()

        assert not path.exists()
        assert bound.directory.closed
        assert bound.listener.fileno() == -1
        assert open_descriptors() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("serve", [False, True])
async def test_runner_preparation_transfers_socket_until_server_closes(
    tmp_path: Path, kora_capture: Capture, serve: bool
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        _,
        arguments,
    ):
        before = open_descriptors()
        tasks = set(asyncio.all_tasks())
        prepared = prepared_at(arguments)
        bound = prepared.bound
        assert bound is not None
        clock = StepClock()
        try:
            await prepared.register(own_identity(), clock)
            bound.listener.setblocking(True)
            server = prepared.server(
                clock,
                NativeIdentityReader(),
                own_identity(),
                kora_capture.diagnostics.logger(COMPONENT),
            )
            try:
                assert prepared.bound is None
                assert not bound.listener.getblocking()
                if serve:
                    server.start()
                    channel = await client(prepared.path, clock)
                    try:
                        status = await asked_status(channel, clock)
                    finally:
                        channel.close()
                    assert status.status.state == "starting"
                    await until(
                        lambda: any(not ready.done() for _, ready in clock.sleepers)
                    )
                    os.unlink(prepared.path)
                    clock.advance(REVERIFY_NS)
                    await until(lambda: bound.directory.closed)
                    assert bound.directory.closed
                    assert bound.listener.fileno() == -1
                assert not prepared.registry.directory.closed
                assert isinstance(prepared.registry.entry(prepared.key), Running)

            finally:
                await server.close()
            assert not prepared.registry.directory.closed
        finally:
            prepared.close()
        guard = os.open(os.devnull, os.O_RDONLY)
        try:
            guarded_identity = os.fstat(guard)

            prepared.close()

            assert os.fstat(guard) == guarded_identity
        finally:
            os.close(guard)
        assert bound.directory.closed
        assert bound.listener.fileno() == -1
        assert prepared.registry.directory.closed
        assert open_descriptors() == before
        assert set(asyncio.all_tasks()) == tasks


@pytest.mark.asyncio
async def test_runner_preparation_owns_socket_before_transfer(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        _,
        arguments,
    ):
        before = open_descriptors()
        prepared = prepared_at(arguments)
        bound = prepared.bound
        assert bound is not None

        prepared.close()
        guard = os.open(os.devnull, os.O_RDONLY)
        try:
            guarded_identity = os.fstat(guard)
            prepared.close()

            assert os.fstat(guard) == guarded_identity
        finally:
            os.close(guard)
        assert bound.listener.fileno() == -1
        assert bound.directory.closed
        assert prepared.registry.directory.closed
        assert open_descriptors() == before


@pytest.mark.asyncio
async def test_runner_preparation_close_releases_directory_after_listener_error(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        _,
        arguments,
    ):
        before = open_descriptors()
        prepared = prepared_at(arguments)
        bound = prepared.bound
        assert bound is not None
        os.close(bound.listener.fileno())

        with pytest.raises(OSError, match="Bad file descriptor") as failed:
            prepared.close()

        assert failed.value.errno == errno.EBADF
        assert bound.directory.closed
        assert prepared.registry.directory.closed
        assert open_descriptors() == before


@pytest.mark.asyncio
async def test_runner_preparation_cancelled_registration_releases_resources(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        _,
        arguments,
    ):
        before = open_descriptors()
        prepared = prepared_at(arguments)
        bound = prepared.bound
        assert bound is not None
        clock = StepClock()
        lock = arguments.state_root / "registry" / f"{prepared.key.name}.lock"
        try:
            with stopped_holder(lock):
                registering = asyncio.create_task(
                    prepared.register(own_identity(), clock)
                )
                try:
                    await until(clock.waiting.is_set)

                    registering.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await registering

                    assert not Path(prepared.path).exists()
                    assert isinstance(prepared.registry.entry(prepared.key), Starting)
                finally:
                    if not registering.done():
                        registering.cancel()
                        await asyncio.gather(registering, return_exceptions=True)
        finally:
            prepared.close()
        assert bound.directory.closed
        assert bound.listener.fileno() == -1
        assert prepared.registry.directory.closed
        assert open_descriptors() == before


@pytest.mark.asyncio
async def test_runner_preparation_failure_after_transfer_releases_resources(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        _,
        arguments,
    ):
        before = open_descriptors()
        prepared = prepared_at(arguments)
        bound = prepared.bound
        assert bound is not None
        try:
            server = prepared.server(
                StepClock(),
                NativeIdentityReader(),
                own_identity(),
                kora_capture.diagnostics.logger(COMPONENT),
            )
            with pytest.raises(FileNotFoundError):
                await refused_start(
                    server, arguments.state_root / "absent-startup-input"
                )
        finally:
            prepared.close()

        assert bound.directory.closed
        assert bound.listener.fileno() == -1
        assert prepared.registry.directory.closed
        assert open_descriptors() == before


async def refused_start(server: ControlServer, missing: Path) -> None:
    try:
        os.stat(missing)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_runner_preparation_constructor_failure_retains_ownership(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with starting_registration(state_root(tmp_path), kora_capture) as (
        _,
        arguments,
    ):
        before = open_descriptors()
        tasks = set(asyncio.all_tasks())
        prepared = prepared_at(arguments)
        bound = prepared.bound
        assert bound is not None
        clock = StepClock()
        server: ControlServer | None = None
        try:
            await prepared.register(own_identity(), clock)
            registered = prepared.registry.entry(prepared.key)
            assert isinstance(registered, Running)
            bound.listener.close()

            with pytest.raises(OSError, match="Bad file descriptor") as failed:
                server = prepared.server(
                    clock,
                    NativeIdentityReader(),
                    own_identity(),
                    kora_capture.diagnostics.logger(COMPONENT),
                )

            assert failed.value.errno == errno.EBADF
            assert any(
                frame.name == "__init__" and Path(frame.filename).name == "serving.py"
                for frame in traceback.extract_tb(failed.value.__traceback__)
            )
            assert prepared.bound is bound
            assert not bound.directory.closed
            assert not prepared.registry.directory.closed
            assert prepared.registry.entry(prepared.key) == registered
        finally:
            try:
                if server is not None:
                    await server.close()
            finally:
                prepared.close()
        guard = os.open(os.devnull, os.O_RDONLY)
        try:
            guarded_identity = os.fstat(guard)
            prepared.close()
            assert os.fstat(guard) == guarded_identity
        finally:
            os.close(guard)
        assert bound.listener.fileno() == -1
        assert bound.directory.closed
        assert prepared.registry.directory.closed
        assert open_descriptors() == before
        assert set(asyncio.all_tasks()) == tasks
