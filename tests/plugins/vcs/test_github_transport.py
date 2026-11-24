import asyncio
import ssl
from dataclasses import dataclass, field
from pathlib import Path

import certifi
import pytest

from kora.plugins.vcs.git.github_http import (
    MAX_RESPONSE_BYTES,
    GitHubTransportError,
    HostResponse,
    github_exchange,
)
from tests.support.https_fixture import Body, Redirect, create_authority, https_server

BEARER = "ghs-synthetic-http-credential"
PATH = "/repos/owner/repository/pulls"


def test_github_transport_refuses_wrong_peer_before_sending_credentials(
    tmp_path: Path,
) -> None:
    authority = create_authority(tmp_path)

    async def send(origin: str, tls: ssl.SSLContext) -> None:
        async with github_exchange(origin, tls, BEARER) as exchange:
            await exchange.get(PATH)

    with https_server(authority, [Body(200, b"[]")]) as host:
        trusted = ssl.create_default_context(cafile=authority.bundle)
        untrusted = ssl.create_default_context(cafile=certifi.where())
        wrong_name = f"https://localhost:{host.netloc.rsplit(':', 1)[1]}"

        with pytest.raises(GitHubTransportError):
            asyncio.run(send(wrong_name, trusted))
        with pytest.raises(GitHubTransportError):
            asyncio.run(send(host.url(""), untrusted))

        assert host.requests == []


def test_github_transport_does_not_follow_redirect(tmp_path: Path) -> None:
    authority = create_authority(tmp_path)
    tls = ssl.create_default_context(cafile=authority.bundle)

    async def send(origin: str) -> HostResponse:
        async with github_exchange(origin, tls, BEARER) as exchange:
            return await exchange.get(PATH)

    with (
        https_server(authority, [Body(200, b"[]")]) as other,
        https_server(authority, [Redirect(other.url(PATH))]) as host,
    ):
        response = asyncio.run(send(host.url("")))

        assert response == HostResponse(302, None, None)
        assert len(host.requests) == 1
        assert host.requests[0].authorization
        assert other.requests == []


def test_github_transport_refuses_oversized_response(tmp_path: Path) -> None:
    authority = create_authority(tmp_path)
    tls = ssl.create_default_context(cafile=authority.bundle)
    payload = b'"' + b"a" * MAX_RESPONSE_BYTES + b'"'

    async def send(origin: str) -> HostResponse:
        async with github_exchange(origin, tls, BEARER) as exchange:
            return await exchange.get(PATH)

    with https_server(authority, [Body(200, payload)]) as host:
        response = asyncio.run(send(host.url("")))

        assert response == HostResponse(200, None, None)
        assert len(host.requests) == 1


@dataclass
class HeldPeer:
    body_response: bool
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if self.body_response:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 128\r\n\r\n[")
                await writer.drain()
            else:
                await reader.readexactly(1)
            self.entered.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            self.finished.set()


@pytest.mark.parametrize("body_response", [False, True], ids=["tls", "body"])
def test_github_transport_cancellation_closes_the_active_socket(
    tmp_path: Path, body_response: bool
) -> None:
    authority = create_authority(tmp_path)
    tls = ssl.create_default_context(cafile=authority.bundle)

    async def inspect() -> tuple[bool, bool]:
        peer = HeldPeer(body_response)
        server = await asyncio.start_server(
            peer.serve,
            "127.0.0.1",
            0,
            ssl=authority.context if body_response else None,
        )
        port = server.sockets[0].getsockname()[1]

        async def send() -> None:
            async with github_exchange(
                f"https://127.0.0.1:{port}", tls, BEARER
            ) as exchange:
                await exchange.get(PATH)

        async with asyncio.timeout(10), server:
            task = asyncio.create_task(send())
            try:
                await peer.entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await peer.finished.wait()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        return peer.finished.is_set(), task.cancelled()

    peer_finished, task_cancelled = asyncio.run(inspect())

    assert peer_finished
    assert task_cancelled
