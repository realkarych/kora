import asyncio
import json
import socket

import pytest

from kora.client import ApiError, ClientError, KoraClient
from kora.client.errors import Failure, Outcome
from kora.client.transport import COMPONENT
from kora.conformance.capture import captured
from kora.observability import Level, LoggingSettings, bound_scope
from kora.observability.context import current_context
from tests.client.support import (
    BOUND,
    CREDENTIAL,
    SCHEMA,
    ProbeClient,
    Reader,
    Writer,
    peer,
    read_request,
    reply,
)
from tests.plugins.authentication.support import AuthorityClock

ERROR_STATUS: bytes = b"HTTP/1.1 409 Conflict"
ERROR_BODY: bytes = (
    b'{ "error":{"code":"API002","message":"Operation conflicts with current state.",'
    b'"location":null,"retryable":false,"details":{"added":true},'
    b'"request_id":"req_saved","additional":"value"}, "future":[1] }'
)
AUTH_REFUSAL: bytes = (
    b'{"error":{"code":"API009","message":"Authentication is required.",'
    b'"location":null,"retryable":false,"details":{},"request_id":"req_refused"}}'
)
AUTH_STATUS: bytes = b"HTTP/1.1 401 Unauthorized"
CORRELATION_HEADER: bytes = b"X-Request-ID: req_current"
INVALID_PREFIX: bytes = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nX-Request-ID: req_partial\r\n\r\n{"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [b"API002", b"API321"])
async def test_client_preserves_api_error_payload_and_additive_fields(
    code: bytes,
) -> None:
    body = ERROR_BODY.replace(b"API002", code)

    async def handle(reader: Reader, writer: Writer) -> None:
        await read_request(reader)
        reply(writer, body, ERROR_STATUS, (CORRELATION_HEADER,))

    async with peer(handle) as origin, KoraClient(origin.url, CREDENTIAL) as client:
        with pytest.raises(ApiError) as refusal:
            await client.openapi()

    assert refusal.value.status == 409
    assert refusal.value.code == code.decode()
    assert refusal.value.body == json.loads(body)
    assert refusal.value.content == body
    assert refusal.value.correlation.request_id == "req_current"
    assert refusal.value.correlation.replayed_request_id == "req_saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "headers", "reason"),
    [
        (ERROR_STATUS, b'{"error":{"code":"API002"}}', (), Failure.STATUS_ENVELOPE),
        (b"HTTP/1.1 400 Bad Request", ERROR_BODY, (), Failure.STATUS_ENVELOPE),
        (ERROR_STATUS, b"<html>", (), Failure.STATUS_ENVELOPE),
        (
            AUTH_STATUS,
            AUTH_REFUSAL.replace(b'"retryable":false', b'"retryable":true'),
            (),
            Failure.STATUS_ENVELOPE,
        ),
        (
            AUTH_STATUS,
            AUTH_REFUSAL.replace(b"Authentication is required.", b"Other message."),
            (),
            Failure.STATUS_ENVELOPE,
        ),
        (b"HTTP/1.1 206 Partial Content", SCHEMA, (), Failure.STATUS_ENVELOPE),
        (b"HTTP/1.1 200 OK", b"{", (), Failure.JSON),
        (b"HTTP/1.1 200 OK", b'{"paths":[]}', (), Failure.SCHEMA),
        (b"HTTP/1.1 200 OK", b'{"paths":{},"paths":[]}', (), Failure.JSON),
        (b"HTTP/1.1 200 OK", b'{"paths":Infinity}', (), Failure.JSON),
        (
            b"HTTP/1.1 200 OK",
            b"x",
            (b"Content-Encoding: gzip",),
            Failure.STATUS_ENVELOPE,
        ),
    ],
)
async def test_client_distinguishes_transport_and_api_failures(
    status: bytes, payload: bytes, headers: tuple[bytes, ...], reason: Failure
) -> None:
    async def handle(reader: Reader, writer: Writer) -> None:
        await read_request(reader)
        reply(writer, payload, status, (*headers, CORRELATION_HEADER))

    async with peer(handle) as origin, KoraClient(origin.url, CREDENTIAL) as client:
        with pytest.raises(ClientError) as refusal:
            await client.openapi()

    assert refusal.value.reason is reason
    assert refusal.value.correlation.request_id == "req_current"
    assert refusal.value.envelope() == {
        "error": {
            "origin": "client",
            "code": "CLT003",
            "message": "The API response was invalid.",
            "location": None,
            "retryable": False,
            "details": {
                "reason": reason,
                "outcome": "unknown",
                "recovery_hint": (
                    "Inspect authoritative state and any remaining local output "
                    "before retrying explicitly."
                ),
            },
            "request_id": None,
        }
    }


@pytest.mark.asyncio
async def test_client_classifies_disconnect_after_send_and_never_resends() -> None:
    seen: list[bytes] = []

    async def handle(reader: Reader, writer: Writer) -> None:
        seen.append(await read_request(reader))
        writer.write(INVALID_PREFIX)

    async with peer(handle) as origin, ProbeClient(origin.url, CREDENTIAL) as client:
        with pytest.raises(ClientError) as refusal:
            await client.probe(
                "POST",
                response_limit=1024,
                body={"effect": True},
            )

    assert len(seen) == 1
    assert seen[0].startswith(b"POST /api/v1/probe HTTP/1.1\r\n")
    assert seen[0].endswith(b'{"effect":true}')
    assert refusal.value.code == "CLT004"
    assert refusal.value.reason is Failure.STREAM
    assert refusal.value.outcome is Outcome.UNKNOWN
    assert refusal.value.correlation.request_id == "req_partial"
    assert refusal.value.envelope()["error"]["request_id"] is None


@pytest.mark.asyncio
async def test_client_classifies_connect_failure_before_send() -> None:
    loop = asyncio.get_running_loop()
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as control,
    ):
        listener.setblocking(False)
        occupant.setblocking(False)
        control.setblocking(False)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = ("127.0.0.1", listener.getsockname()[1])
        await asyncio.wait_for(loop.sock_connect(occupant, address), BOUND)
        established, _ = await asyncio.wait_for(loop.sock_accept(listener), BOUND)
        with established:
            listener.close()
            with pytest.raises(ConnectionRefusedError):
                await asyncio.wait_for(loop.sock_connect(control, address), BOUND)

            async with KoraClient(
                f"http://127.0.0.1:{address[1]}", CREDENTIAL
            ) as client:
                with pytest.raises(ClientError) as refusal:
                    await client.openapi()

    assert refusal.value.reason is Failure.CONNECTION
    assert refusal.value.outcome is Outcome.NOT_SENT
    assert refusal.value.code == "CLT001"
    assert refusal.value.envelope()["error"]["request_id"] is None


@pytest.mark.asyncio
async def test_client_bounds_chunked_response_before_completion() -> None:
    connected = asyncio.Event()

    async def handle(reader: Reader, writer: Writer) -> None:
        await read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        writer.write(b"41\r\n" + b"x" * 65 + b"\r\n")
        assert await asyncio.wait_for(reader.read(), BOUND) == b""
        connected.set()

    async with peer(handle) as origin, ProbeClient(origin.url, CREDENTIAL) as client:
        with pytest.raises(ClientError) as refusal:
            await client.probe("GET", response_limit=64)
        await asyncio.wait_for(connected.wait(), BOUND)

    assert refusal.value.reason is Failure.RESPONSE_LIMIT
    assert refusal.value.outcome is Outcome.UNKNOWN


@pytest.mark.asyncio
async def test_client_diagnostics_are_content_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    poison = b"private-peer-content"
    call = 0

    async def handle(reader: Reader, writer: Writer) -> None:
        nonlocal call
        await read_request(reader)
        call += 1
        if call == 1:
            reply(writer, ERROR_BODY, ERROR_STATUS, (CORRELATION_HEADER,))
        elif call == 2:
            reply(
                writer,
                poison,
                headers=(
                    b"X-Request-ID: " + poison,
                    b"Set-Cookie: peer=" + CREDENTIAL.encode(),
                ),
            )
        else:
            writer.write(INVALID_PREFIX)

    settings = LoggingSettings(level=Level.DEBUG, third_party={"httpcore": Level.DEBUG})
    with captured(COMPONENT, settings, "client-test", AuthorityClock().now) as capture:
        async with (
            peer(handle) as origin,
            KoraClient(
                origin.url, CREDENTIAL, diagnostics=capture.diagnostics
            ) as client,
        ):
            with bound_scope(request_id="req_parent", replayed_request_id="req_old"):
                with pytest.raises(ApiError):
                    await client.openapi()
                with pytest.raises(ClientError):
                    await client.openapi()
                with pytest.raises(ClientError):
                    await client.openapi()
                ambient = current_context()
        records = capture.finish()

    completions = [
        record for record in records if record["event"] == "client.request.completed"
    ]
    assert ambient == {"request_id": "req_parent", "replayed_request_id": "req_old"}
    assert len(completions) == 3
    assert completions[0]["request_id"] == "req_current"
    assert completions[0]["replayed_request_id"] == "req_saved"
    assert completions[0]["outcome"] == "api_error"
    assert "request_id" not in completions[1]
    assert "replayed_request_id" not in completions[1]
    assert completions[1]["code"] == "CLT003"
    assert completions[2]["request_id"] == "req_partial"
    assert completions[2]["code"] == "CLT004"
    assert CREDENTIAL not in str(records)
    assert poison.decode() not in str(records)
    assert any(record.name == "httpcore.http11" for record in caplog.records)
    assert CREDENTIAL not in caplog.text
    assert poison.decode() not in caplog.text
