import asyncio
import json
import os
import socket
import struct
from collections.abc import Iterator
from types import CoroutineType
from typing import cast

import pytest
from pydantic import ValidationError

from kora.__main__ import InstalledPluginDiscovery
from kora.cli import CLI_COMPONENT
from kora.cli.plugins import DiscoveryComplete, ListingError
from kora.conformance.transport import LifetimeClient
from kora.observability import LoggingSettings, configure_process
from kora.protocols.time import SystemClock
from kora.runner.channels import (
    BoundedChannel,
    ChannelError,
    ChannelFailure,
    cancel_task,
    wait_future,
)
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import ProbeClock

pytestmark = pytest.mark.asyncio


async def test_channel_wait_keeps_cancellation_during_timer_cleanup() -> None:
    clock = ProbeClock()
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    operation = asyncio.create_task(
        wait_future(ready, clock.monotonic_ns() + 30_000_000_000, clock)
    )
    try:
        await asyncio.wait_for(clock.changed.wait(), 5)
        waiter = next(iter(clock.waiters))
        waiter.add_done_callback(lambda _: operation.cancel())

        ready.set_result(None)
        done, _ = await asyncio.wait((operation,), timeout=5)

        assert operation in done
        assert operation.cancelled()
        assert not clock.waiters
    finally:
        clock.use_system_time()
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_diagnostic_reader_keeps_cancellation_during_timer_cleanup() -> None:
    clock = ProbeClock()
    first, second = socket.socketpair()
    first.setblocking(False)
    event = {
        "schema_version": 1,
        "timestamp": "2026-10-26T00:00:00Z",
        "component": "protocols.loading",
        "logger": "protocols.loading",
        "instance_id": "a" * 32,
        "level": "WARNING",
        "event": "protocols.loading.provider_unavailable",
        "message": "an installed provider entry point could not be loaded",
        "entry_name_length": 1,
        "code": "provider_load_failed",
    }
    with open(os.devnull, "wb") as sink:
        diagnostics = configure_process(
            CLI_COMPONENT,
            LoggingSettings(),
            "listing-cancellation",
            clock.now,
            sink.fileno(),
        )
        discovery = InstalledPluginDiscovery(diagnostics, clock)
        operation = asyncio.create_task(
            discovery.read_events(first, clock.monotonic_ns() + 30_000_000_000)
        )
        try:
            await asyncio.wait_for(clock.changed.wait(), 5)
            waiter = next(iter(clock.waiters))
            waiter.add_done_callback(lambda _: operation.cancel())

            second.sendall(json.dumps(event).encode() + b"\n")
            done, _ = await asyncio.wait((operation,), timeout=5)

            assert operation in done
            assert operation.cancelled()
            assert not clock.waiters
        finally:
            clock.use_system_time()
            second.close()
            first.close()
            operation.cancel()
            try:
                await asyncio.gather(operation, return_exceptions=True)
            finally:
                diagnostics.shutdown()


async def test_owned_task_fault_keeps_simultaneous_caller_cancellation() -> None:
    entered = asyncio.Event()
    held = asyncio.Event()

    async def failing() -> None:
        entered.set()
        try:
            await held.wait()
        finally:
            raise ValueError("owned_task_cleanup_failed")

    target = asyncio.create_task(failing())
    await entered.wait()
    operation = asyncio.create_task(cancel_task(target))
    target.add_done_callback(lambda _: operation.cancel())

    try:
        with pytest.raises(ValueError, match="owned_task_cleanup_failed") as failure:
            await operation

        assert isinstance(failure.value.__context__, asyncio.CancelledError)
        assert target.done()
    finally:
        target.cancel()
        operation.cancel()
        await asyncio.gather(target, operation, return_exceptions=True)


async def test_client_cancellation_joins_its_sibling_deadline_timer() -> None:
    baseline = frozenset(asyncio.all_tasks())
    clock = ProbeClock()
    first, second = socket.socketpair()
    client = LifetimeClient(first, clock=clock)
    closing = asyncio.create_task(client.close(None))
    loop = asyncio.get_running_loop()

    try:
        async with asyncio.timeout(5):
            while True:
                clock.changed.clear()
                changed = tuple(
                    cast(asyncio.Task[None], task)
                    for task in asyncio.all_tasks()
                    if isinstance(coroutine := task.get_coro(), CoroutineType)
                    and coroutine.cr_code is asyncio.Event.wait.__code__
                )
                if len(changed) == 1 and clock.waiters:
                    break
                await clock.changed.wait()
            changed[0].add_done_callback(lambda _: loop.call_soon(client.writer.cancel))

            client.changed.set()
            done, _ = await asyncio.wait((client.writer,), timeout=5)

            assert client.writer in done
            assert client.writer.cancelled()
            assert not clock.waiters
    finally:
        client.disconnect()
        owned = tuple(asyncio.all_tasks() - baseline)
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, closing, return_exceptions=True)
        second.close()

    assert not client.pending
    assert not client.resources
    assert client.control_bytes == client.execution_bytes == 0
    assert client.reader.done()


@pytest.fixture
def discovery() -> Iterator[InstalledPluginDiscovery]:
    with open(os.devnull, "wb") as sink:
        clock = SystemClock()
        diagnostics = configure_process(
            CLI_COMPONENT,
            LoggingSettings(),
            "listing-channel",
            clock.now,
            sink.fileno(),
        )
        try:
            yield InstalledPluginDiscovery(diagnostics, clock)
        finally:
            diagnostics.shutdown()


def record(payload: bytes) -> bytes:
    return struct.pack("!III", 4, len(payload), len(payload)) + payload


async def send_bytes(endpoint: socket.socket, payload: bytes) -> None:
    endpoint.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_sendall(endpoint, payload)
    finally:
        endpoint.close()


@pytest.mark.parametrize(
    "trailing", [b"", b"\x00", b"\x00\x00", b"\x00\x00\x00", b"\x00" * 4]
)
async def test_listing_requires_exact_result_eof(
    discovery: InstalledPluginDiscovery, trailing: bytes
) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 1_000_000_000
    first, second = socket.socketpair()
    receiver = BoundedChannel(first, clock)
    complete = DiscoveryComplete(providers=0, failures=0, diagnostics_ok=True)
    second.sendall(record(complete.model_dump_json().encode()) + trailing)
    second.close()

    try:
        if trailing:
            with pytest.raises(ChannelError) as refused:
                await discovery.read_result(receiver, deadline)
            reason = refused.value.reason
            result = None
        else:
            result = await discovery.read_result(receiver, deadline)
            reason = None
    finally:
        receiver.close()

    assert reason is (ChannelFailure.MALFORMED if trailing else None)
    assert (result is None) is bool(trailing)
    assert receiver.closed


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (record(b'{"kind":"unknown"}'), ValidationError),
        (
            record(
                b'{"kind":"provider","protocol":"policy","name":"valid","version":"1"}'
            ),
            ChannelError,
        ),
        (
            record(
                b'{"kind":"complete","providers":1,"failures":0,"diagnostics_ok":true}'
            ),
            ListingError,
        ),
        (
            record(
                b'{"kind":"complete","providers":0,"failures":0,"diagnostics_ok":false}'
            ),
            ListingError,
        ),
        (struct.pack("!II", 4, 4097), ChannelError),
        (struct.pack("!III", 4, 100, 100) + b"{", ChannelError),
    ],
)
async def test_listing_refuses_incomplete_or_invalid_results(
    discovery: InstalledPluginDiscovery, payload: bytes, error: type[Exception]
) -> None:
    clock = SystemClock()
    first, second = socket.socketpair()
    receiver = BoundedChannel(first, clock)
    second.sendall(payload)
    second.close()

    try:
        with pytest.raises(error):
            await discovery.read_result(receiver, clock.monotonic_ns() + 1_000_000_000)
    finally:
        receiver.close()

    assert receiver.closed


@pytest.mark.parametrize(
    ("changed", "ending", "error"),
    [
        ({"component": "cli"}, b"\n", ValidationError),
        ({"payload": "private"}, b"\n", ValidationError),
        ({"exception_type": "private secret value"}, b"\n", ValidationError),
        ({"distribution": "x" * 17000}, b"\n", ListingError),
        ({}, b"", ListingError),
    ],
)
async def test_listing_refuses_forged_or_truncated_diagnostics(
    discovery: InstalledPluginDiscovery,
    changed: dict[str, str],
    ending: bytes,
    error: type[Exception],
) -> None:
    clock = SystemClock()
    first, second = socket.socketpair()
    first.setblocking(False)
    event = {
        "schema_version": 1,
        "timestamp": "2026-10-26T00:00:00Z",
        "component": "protocols.loading",
        "logger": "protocols.loading",
        "instance_id": "a" * 32,
        "level": "WARNING",
        "event": "protocols.loading.provider_unavailable",
        "message": "an installed provider entry point could not be loaded",
        "entry_name_length": 1,
        "code": "provider_load_failed",
        **changed,
    }
    writer = asyncio.create_task(
        send_bytes(second, json.dumps(event).encode() + ending)
    )

    try:
        with pytest.raises(error):
            await discovery.read_events(first, clock.monotonic_ns() + 1_000_000_000)
    finally:
        first.close()
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

    assert first.fileno() == -1
