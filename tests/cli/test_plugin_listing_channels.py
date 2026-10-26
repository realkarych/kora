import asyncio
import json
import os
import socket
import struct
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from kora.__main__ import InstalledPluginDiscovery
from kora.cli import CLI_COMPONENT
from kora.cli.plugins import DiscoveryComplete, ListingError
from kora.observability import LoggingSettings, configure_process
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure

pytestmark = pytest.mark.asyncio


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
