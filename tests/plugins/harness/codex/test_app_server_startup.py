import asyncio
import json
import traceback
from pathlib import Path
from typing import Literal

import pytest

from kora.plugins.harness.codex.app_server_rpc import RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_startup import (
    ExecutionOs,
    StartupError,
    StartupFailure,
    StartupIdentity,
    initialize_app_server,
)
from tests.plugins.harness.codex.probe import Stdio, report_connection_failure
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

EXPECTED_HOME = Path("/fixture/codex")
VERSION = "0.166.0"
CLIENT = "kora-startup-test"
CLIENT_VERSION = "1"
PRIVATE_AGENT = "peer agent canary"
PRIVATE_PATH = "/peer/private path canary"


def expected_identity(platform_os: ExecutionOs) -> StartupIdentity:
    return StartupIdentity(VERSION, platform_os, EXPECTED_HOME, CLIENT, CLIENT_VERSION)


def initialize_response(platform_os: ExecutionOs) -> dict[str, object]:
    return {
        "userAgent": f"{CLIENT}/{VERSION} (fixture os) fixture-client ({CLIENT}; 1)",
        "platformFamily": "unix",
        "platformOs": platform_os,
        "codexHome": str(EXPECTED_HOME),
    }


def response_frame(response: object) -> bytes:
    return json.dumps({"id": 1, "result": response}).encode() + b"\n"


@pytest.mark.parametrize(
    ("platform_os", "experimental"), [("linux", True), ("macos", False)]
)
@pytest.mark.asyncio
async def test_app_server_startup_checks_identity_before_acknowledging(
    platform_os: ExecutionOs, experimental: bool
) -> None:
    async with wire() as connection:
        expected = expected_identity(platform_os)
        response = response_frame(initialize_response(platform_os))
        handshake = connection.start(
            initialize_app_server(
                connection.rpc,
                expected,
                deadline=future_deadline(),
                experimental_api=experimental,
            )
        )

        request = await connection.peer_reader.readline()
        await connection.send(response)
        version = await handshake
        ack = await connection.peer_reader.readline()

        params: dict[str, object] = {
            "clientInfo": {"name": CLIENT, "version": CLIENT_VERSION}
        }
        if experimental:
            params["capabilities"] = {"experimentalApi": True}
        assert json.loads(request) == {
            "id": 1,
            "method": "initialize",
            "params": params,
        }
        assert json.loads(ack) == {"method": "initialized"}
        assert version == VERSION


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"userAgent": f"{CLIENT}/0.0.0 (os) ({CLIENT}; {VERSION})"},
            StartupFailure.VERSION_MISMATCH,
        ),
        ({"userAgent": f"other/{VERSION} (os)"}, StartupFailure.VERSION_MISMATCH),
        ({"userAgent": f"{CLIENT}/{VERSION}0 (os)"}, StartupFailure.VERSION_MISMATCH),
        ({"userAgent": f"{CLIENT}/{VERSION}"}, StartupFailure.VERSION_MISMATCH),
        ({"platformFamily": "windows"}, StartupFailure.PLATFORM_MISMATCH),
        ({"platformOs": "macos"}, StartupFailure.PLATFORM_MISMATCH),
        ({"codexHome": "fixture/codex"}, StartupFailure.HOME_MISMATCH),
        ({"codexHome": "/fixture/./codex"}, StartupFailure.HOME_MISMATCH),
        ({"codexHome": "/fixture/other"}, StartupFailure.HOME_MISMATCH),
        ({"userAgent": 1}, StartupFailure.INVALID_RESPONSE),
        ({"platformFamily": []}, StartupFailure.INVALID_RESPONSE),
        ({"platformOs": {}}, StartupFailure.INVALID_RESPONSE),
        ({"codexHome": None}, StartupFailure.INVALID_RESPONSE),
    ],
    ids=[
        "client-suffix",
        "originator",
        "version-prefix",
        "no-delimiter",
        "platform-family",
        "platform-os",
        "relative-home",
        "different-spelling",
        "different-home",
        "agent-type",
        "family-type",
        "os-type",
        "home-type",
    ],
)
@pytest.mark.asyncio
async def test_app_server_startup_refuses_inconsistent_identity_without_followup(
    overrides: dict[str, object], reason: StartupFailure
) -> None:
    async with wire() as connection:
        response = response_frame({**initialize_response("linux"), **overrides})
        handshake = connection.start(
            initialize_app_server(
                connection.rpc, expected_identity("linux"), deadline=future_deadline()
            )
        )
        request = await connection.peer_reader.readline()

        await connection.send(response)
        with pytest.raises(StartupError) as caught:
            await handshake
        connection.writer.write_eof()
        followup = await connection.peer_reader.read()

        assert json.loads(request)["method"] == "initialize"
        assert caught.value.reason is reason
        assert followup == b""


def response_without(field: str) -> dict[str, object]:
    response = initialize_response("linux")
    del response[field]
    return response


@pytest.mark.parametrize(
    "incomplete",
    [
        pytest.param({}, id="empty-object"),
        pytest.param([], id="list"),
        pytest.param(None, id="null"),
        pytest.param(response_without("userAgent"), id="missing-agent"),
        pytest.param(response_without("platformFamily"), id="missing-family"),
        pytest.param(response_without("platformOs"), id="missing-os"),
        pytest.param(response_without("codexHome"), id="missing-home"),
    ],
)
@pytest.mark.asyncio
async def test_app_server_startup_refuses_missing_reply_shape(
    incomplete: object,
) -> None:
    async with wire() as connection:
        handshake = connection.start(
            initialize_app_server(
                connection.rpc, expected_identity("linux"), deadline=future_deadline()
            )
        )
        await connection.peer_reader.readline()

        await connection.send(response_frame(incomplete))
        with pytest.raises(StartupError) as caught:
            await handshake
        connection.writer.write_eof()
        followup = await connection.peer_reader.read()

        assert caught.value.reason is StartupFailure.INVALID_RESPONSE
        assert followup == b""


@pytest.mark.parametrize("field", ["userAgent", "codexHome"])
@pytest.mark.asyncio
async def test_app_server_startup_failure_diagnostics_are_content_free(
    field: str, capsys: pytest.CaptureFixture[str]
) -> None:
    async with wire() as connection:
        response = initialize_response("linux")
        response[field] = PRIVATE_AGENT if field == "userAgent" else PRIVATE_PATH
        handshake = connection.start(
            initialize_app_server(
                connection.rpc, expected_identity("linux"), deadline=future_deadline()
            )
        )
        await connection.peer_reader.readline()

        await connection.send(response_frame(response))
        with pytest.raises(StartupError) as caught:
            await handshake
        status = report_connection_failure(caught.value)
        captured = capsys.readouterr()
        rendered = "".join(traceback.format_exception(caught.value))

        assert status == 1
        assert captured.out == ""
        assert json.loads(captured.err) == {
            "level": "ERROR",
            "event": "probe.case_failed",
            "detail": caught.value.reason.value,
        }
        assert caught.value.args == (caught.value.reason.value,)
        assert caught.value.__context__ is None
        assert PRIVATE_AGENT not in captured.err + rendered
        assert PRIVATE_PATH not in captured.err + rendered


@pytest.mark.asyncio
async def test_app_server_startup_does_not_record_pre_reply_terminal() -> None:
    async with wire() as connection:
        stdio = Stdio(connection.rpc, future_deadline(), expected_identity("linux"))
        observation = connection.start(
            stdio.observe(Path("/fixture"), "implicit_account")
        )
        await connection.peer_reader.readline()

        await connection.send(
            b'{"method":"turn/completed","params":{"threadId":"unrelated"}}\n',
            response_frame(initialize_response("linux")),
        )
        ack = await connection.peer_reader.readline()
        followup = await connection.peer_reader.readline()
        await connection.send(
            b'{"id":2,"result":{"account":null,"requiresOpenaiAuth":true}}\n'
        )
        await observation

        assert json.loads(ack) == {"method": "initialized"}
        assert json.loads(followup)["method"] == "account/read"
        assert stdio.terminal is None


@pytest.mark.parametrize("outcome", ["eof", "deadline", "cancel"])
@pytest.mark.asyncio
async def test_app_server_startup_preserves_transport_abort(
    outcome: Literal["eof", "deadline", "cancel"],
) -> None:
    deadline = future_deadline()
    observed_time = asyncio.get_running_loop().time()

    def clock() -> float:
        return observed_time

    async with wire(clock=clock) as connection:
        handshake = connection.start(
            initialize_app_server(
                connection.rpc, expected_identity("linux"), deadline=deadline
            )
        )
        await connection.peer_reader.readline()

        if outcome == "cancel":
            handshake.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handshake
        else:
            if outcome == "eof":
                connection.peer_writer.write_eof()
            else:
                observed_time = deadline
                await connection.send(response_frame(initialize_response("linux")))
            with pytest.raises(RpcError) as caught:
                await handshake
            expected = (
                RpcFailure.DISCONNECTED if outcome == "eof" else RpcFailure.DEADLINE
            )
            assert caught.value.reason is expected
        connection.writer.write_eof()
        followup = await connection.peer_reader.read()

        assert followup == b""
