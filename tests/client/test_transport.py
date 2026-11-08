import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

from kora.client import ApiError, ClientError, KoraClient
from kora.client.errors import Failure, Outcome
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.models.errors import ApiErrorEnvelope
from tests.api.support import api_capture, application
from tests.client.support import (
    BOUND,
    CREDENTIAL,
    Reader,
    Writer,
    peer,
    read_request,
    reply,
    running_app,
    tls_pair,
)
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture"]


@pytest.mark.asyncio
async def test_client_reads_protected_openapi(
    database: Database, api_capture: Capture
) -> None:
    credential = await seed_case(database, "shared-client")
    authorization = credential.request.authorization
    assert authorization is not None
    app = application(database, api_capture)

    async with running_app(app) as origin:
        async with KoraClient(origin, authorization.removeprefix("Bearer ")) as client:
            schema = await client.openapi()
        async with KoraClient(origin, CREDENTIAL) as denied:
            with pytest.raises(ApiError) as refusal:
                await denied.openapi()

    assert isinstance(app.application, FastAPI)
    assert schema == app.application.openapi()
    assert refusal.value.status == 401
    envelope = ApiErrorEnvelope.model_validate_json(refusal.value.content)
    assert envelope.error.code == "API009"
    assert refusal.value.correlation.request_id == envelope.error.request_id
    assert refusal.value.correlation.replayed_request_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.2", "127.1", "2130706433", "127.0.0.1.", "example.invalid"],
)
async def test_client_rejects_remote_cleartext_before_sending_credentials(
    host: str,
) -> None:
    seen: list[bytes] = []

    async def handle(reader: Reader, writer: Writer) -> None:
        seen.append(await read_request(reader))
        reply(writer)

    async with peer(handle) as origin:
        with pytest.raises(ClientError) as refusal:
            KoraClient(origin.origin(host), CREDENTIAL)

    assert refusal.value.reason is Failure.DESTINATION_POLICY
    assert refusal.value.outcome is Outcome.NOT_SENT
    assert seen == []


@pytest.mark.asyncio
async def test_client_bypasses_ambient_proxy_and_tls_settings(tmp_path: Path) -> None:
    context, bundle = tls_pair(tmp_path)
    keylog = tmp_path / "session-keys"
    origin_requests: list[bytes] = []
    proxy_requests: list[bytes] = []

    async def handle_origin(reader: Reader, writer: Writer) -> None:
        origin_requests.append(await read_request(reader))
        reply(writer)

    async def handle_proxy(reader: Reader, writer: Writer) -> None:
        proxy_requests.append(await read_request(reader))
        reply(writer)

    async with (
        peer(handle_origin) as cleartext,
        peer(handle_origin, context) as tls,
        peer(handle_proxy) as proxy,
    ):
        env = {
            name: value
            for name, value in os.environ.items()
            if name.upper()
            not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        }
        env.update(
            HTTP_PROXY=proxy.url,
            HTTPS_PROXY=proxy.url,
            ALL_PROXY=proxy.url,
            NO_PROXY="",
            SSL_CERT_FILE=str(bundle),
            SSLKEYLOGFILE=str(keylog),
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.client.environment_probe",
            cleartext.url,
            tls.url,
            str(bundle),
            env=env,
        )
        try:
            exit_code = await asyncio.wait_for(child.wait(), BOUND)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        with pytest.raises(ClientError) as explicit:
            KoraClient(cleartext.url, CREDENTIAL, proxy=proxy.url)

    assert exit_code == 0
    assert len(origin_requests) == 2
    assert all(
        b"authorization: bearer " + CREDENTIAL.encode() in request.lower()
        for request in origin_requests
    )
    assert len(proxy_requests) == 1
    assert b"/proxy-control" in proxy_requests[0]
    assert CREDENTIAL.encode() not in proxy_requests[0]
    assert explicit.value.reason is Failure.PROXY_POLICY
    assert explicit.value.outcome is Outcome.NOT_SENT
    assert not keylog.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", ["scheme", "host", "port", "same_origin"])
async def test_client_keeps_bearer_within_verified_origin(
    tmp_path: Path, redirect: str
) -> None:
    context, bundle = tls_pair(tmp_path, include_localhost=True)
    initial: list[bytes] = []
    leaked: list[bytes] = []
    destination: str = ""

    async def origin_response(reader: Reader, writer: Writer) -> None:
        request = await read_request(reader)
        if b"/redirect-target" in request:
            leaked.append(request)
            reply(writer)
            return
        initial.append(request)
        reply(
            writer,
            b"",
            b"HTTP/1.1 307 Temporary Redirect",
            (b"Location: " + destination.encode(),),
        )

    async def redirect_target(reader: Reader, writer: Writer) -> None:
        leaked.append(await read_request(reader))
        reply(writer)

    async with (
        peer(origin_response, context) as origin,
        peer(redirect_target, context if redirect == "port" else None) as target,
    ):
        destination = {
            "scheme": target.url,
            "host": origin.origin("localhost"),
            "port": target.url,
            "same_origin": origin.url,
        }[redirect] + "/redirect-target"
        async with KoraClient(origin.url, CREDENTIAL, ca_bundle=bundle) as client:
            with pytest.raises(ClientError) as refusal:
                await client.openapi()

    assert len(initial) == 1
    assert CREDENTIAL.encode() in initial[0]
    assert refusal.value.reason is Failure.STATUS_ENVELOPE
    assert leaked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["unknown_root", "wrong_host"])
async def test_client_checks_tls_before_sending_credentials(
    tmp_path: Path, problem: str
) -> None:
    context, bundle = tls_pair(tmp_path)
    seen: list[bytes] = []

    async def handle(reader: Reader, writer: Writer) -> None:
        seen.append(await read_request(reader))
        reply(writer)

    async with peer(handle, context) as origin:
        address = origin.origin("localhost") if problem == "wrong_host" else origin.url
        trust = bundle if problem == "wrong_host" else None
        async with KoraClient(address, CREDENTIAL, ca_bundle=trust) as client:
            with pytest.raises(ClientError) as refusal:
                await client.openapi()

    assert refusal.value.reason is Failure.TLS
    assert refusal.value.outcome is Outcome.NOT_SENT
    assert seen == []
