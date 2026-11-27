import json
import os
import sys
from pathlib import Path

import pytest

from kora.cli.open_console import launch_browser
from kora.client import KoraClient
from kora.client.errors import ClientError, Failure
from kora.models.console import LOGIN_LINK_PATH, REDEEM_PATH, LoginSelection
from tests.cli.test_deploy_command import kora
from tests.client.support import (
    CREDENTIAL,
    Reader,
    Writer,
    peer,
    read_request,
    reply,
    tls_pair,
)

SELECTED = LoginSelection(namespace="default", run_id="run_" + "0" * 26)
ONE_TIME_CODE = "a" * 64
EXCHANGE_PATH = REDEEM_PATH.format(code=ONE_TIME_CODE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["foreign-port", "query", "encoded-path", "extra-segment"]
)
async def test_open_console_client_only_returns_the_exact_login_link(
    tmp_path: Path, value: str
) -> None:

    context, bundle = tls_pair(tmp_path)
    requests: list[bytes] = []

    async def handle(reader: Reader, writer: Writer) -> None:
        requests.append(await read_request(reader))
        port = entry.port
        origin = entry.url
        urls = {
            "foreign-port": f"https://127.0.0.1:{1 if port != 1 else 2}{EXCHANGE_PATH}",
            "query": origin + EXCHANGE_PATH + "?other=1",
            "encoded-path": origin + EXCHANGE_PATH.replace("/api/", "/%61pi/"),
            "extra-segment": origin + EXCHANGE_PATH + "/extra",
        }
        reply(
            writer, json.dumps({"url": urls[value]}).encode(), b"HTTP/1.1 201 Created"
        )
        await writer.drain()

    async with (
        peer(handle, context) as entry,
        KoraClient(entry.url, CREDENTIAL, ca_bundle=bundle) as client,
    ):
        with pytest.raises(ClientError) as rejected:
            await client.issue_console_login_link(SELECTED)

    assert rejected.value.reason is Failure.SCHEMA
    [request] = requests
    header, body = request.split(b"\r\n\r\n")
    assert header.startswith(f"POST {LOGIN_LINK_PATH} HTTP/1.1\r\n".encode())
    assert json.loads(body) == SELECTED.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [True, False])
async def test_kora_open_link_failure_uses_fixed_output(
    tmp_path: Path, trusted: bool
) -> None:

    context, bundle = tls_pair(
        tmp_path, address="127.0.0.1" if trusted else "127.0.0.2"
    )
    requests: list[bytes] = []

    async def handle(reader: Reader, writer: Writer) -> None:
        requests.append(await read_request(reader))
        body = json.dumps(
            {
                "error": {
                    "code": "API009",
                    "message": "Authentication is required.",
                    "location": None,
                    "retryable": False,
                    "details": {},
                    "request_id": "req_refused",
                }
            }
        ).encode()
        reply(writer, body, b"HTTP/1.1 401 Unauthorized")
        await writer.drain()

    async with peer(handle, context) as endpoint:
        result = await kora(
            ["open", SELECTED.run_id, "--namespace", SELECTED.namespace],
            {
                "KORA_URL": endpoint.url,
                "KORA_TOKEN": CREDENTIAL,
                "KORA_CA_BUNDLE": str(bundle),
            },
        )

    assert len(requests) == int(trusted)
    assert result.code == 1
    assert result.stdout == b""
    assert [
        line for line in result.stderr.splitlines() if not line.startswith(b"{")
    ] == [b"kora: could not obtain a browser sign-in link."]
    assert endpoint.url.encode() not in result.stderr
    assert CREDENTIAL.encode() not in result.stderr


def test_native_opener_does_not_inherit_private_descriptors(tmp_path: Path) -> None:
    private = tmp_path / "private-marker"
    private.write_bytes(b"private native file")
    opener = tmp_path / "opener"
    with private.open("rb") as marker:
        descriptor = marker.fileno()
        os.set_inheritable(descriptor, True)
        expected_identity = os.fstat(descriptor).st_ino
        opener.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "try:\n"
            f"    identity = os.fstat({descriptor}).st_ino\n"
            "except OSError:\n"
            "    sys.exit(0)\n"
            f"sys.exit(int(identity == {expected_identity}))\n",
            encoding="utf-8",
        )
        opener.chmod(0o700)

        outcome = launch_browser("https://127.0.0.1/", str(opener), {})

        assert outcome is True
        assert os.get_inheritable(descriptor)
        assert marker.read() == b"private native file"
