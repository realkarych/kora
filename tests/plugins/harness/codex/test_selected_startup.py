import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

import pytest

from kora.plugins.harness.codex.app_server_rpc import RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_startup import StartupIdentity
from kora.plugins.harness.codex.app_server_turn import TurnError, TurnFailure
from kora.plugins.harness.codex.selected_startup import (
    SelectedEnvironment,
    SelectedStartupError,
    SelectedStartupFailure,
    prepare_selected_thread,
)
from tests.plugins.harness.codex.wire_fixture import Wire, future_deadline, wire

IDENTITY = StartupIdentity("0.166.0", "linux", Path("/fixture/codex"), "kora-test", "1")
SELECTED = SelectedEnvironment(
    "exec-fixture",
    "ws://127.0.0.1:12345",
    Path("/bin/bash"),
    Path("/fixture/work :@"),
    "fixture-model",
)
BEARER = "z" * 43
THREAD_ID = "018f0302-3491-7b42-92c0-9d15c2933393"
PRIVATE_CANARY = "private-native-reply-canary"
OMITTED = object()


@dataclass(frozen=True)
class Exchange:
    method: str
    params: dict[str, object]
    result: object


def thread_fields() -> dict[str, object]:
    cwd = str(SELECTED.workspace)
    return {
        "cwd": cwd,
        "model": SELECTED.model,
        "modelProvider": "kora_responses",
        "runtimeWorkspaceRoots": [cwd],
        "approvalPolicy": "never",
        "thread": {
            "id": THREAD_ID,
            "cwd": cwd,
            "model": SELECTED.model,
            "modelProvider": "kora_responses",
            "ephemeral": True,
            "forkedFromId": None,
            "parentThreadId": None,
            "turns": [],
            "environments": [
                {
                    "environmentId": SELECTED.environment_id,
                    "cwd": cwd,
                    "runtimeWorkspaceRoots": [cwd],
                }
            ],
        },
    }


def selected_exchanges() -> tuple[Exchange, ...]:
    environment: dict[str, object] = {"environmentId": SELECTED.environment_id}
    cwd = str(SELECTED.workspace)
    return (
        Exchange(
            "environment/status",
            {"environmentId": "local"},
            {"status": "unknown", "error": PRIVATE_CANARY},
        ),
        Exchange(
            "environment/add",
            {
                **environment,
                "execServerUrl": SELECTED.endpoint,
                "authBearerToken": BEARER,
            },
            {},
        ),
        Exchange(
            "environment/info",
            environment,
            {
                "shell": {"name": "bash", "path": str(SELECTED.bash)},
                "cwd": "file:///fixture/work%20:@",
            },
        ),
        Exchange("environment/status", environment, {"status": "ready"}),
        Exchange(
            "thread/start",
            {
                "cwd": cwd,
                "runtimeWorkspaceRoots": [cwd],
                "ephemeral": True,
                "environments": [{**environment, "cwd": cwd}],
            },
            thread_fields(),
        ),
    )


def frame(value: dict[str, object]) -> bytes:
    return json.dumps(value).encode("utf-8") + b"\n"


async def initialize(connection: Wire) -> None:
    request = await connection.peer_reader.readline()
    assert json.loads(request) == {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "kora-test", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    }
    await connection.send(
        frame(
            {
                "id": 1,
                "result": {
                    "userAgent": "kora-test/0.166.0 (fixture)",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                    "codexHome": "/fixture/codex",
                },
            }
        )
    )
    assert json.loads(await connection.peer_reader.readline()) == {
        "method": "initialized"
    }


async def receive(connection: Wire, index: int, exchange: Exchange) -> None:
    request = await connection.peer_reader.readline()
    assert json.loads(request) == {
        "id": index,
        "method": exchange.method,
        "params": exchange.params,
    }


async def answer(connection: Wire, index: int, exchange: Exchange) -> None:
    await receive(connection, index, exchange)
    await connection.send(frame({"id": index, "result": exchange.result}))


async def remaining_bytes(connection: Wire) -> bytes:
    connection.writer.write_eof()
    return await connection.peer_reader.read()


@pytest.mark.asyncio
async def test_selected_startup_prepares_exact_ephemeral_thread_without_a_turn() -> (
    None
):
    exchanges = selected_exchanges()
    async with wire() as connection:
        startup = connection.start(
            prepare_selected_thread(
                connection.rpc, IDENTITY, SELECTED, BEARER, deadline=future_deadline()
            )
        )

        await initialize(connection)
        for index, exchange in enumerate(exchanges, 2):
            await answer(connection, index, exchange)
        thread_id = await startup
        trailing = await remaining_bytes(connection)

        assert thread_id == THREAD_ID
        assert trailing == b""


@pytest.mark.parametrize(
    ("index", "result", "reason"),
    [
        (0, {"status": "ready"}, SelectedStartupFailure.LOCAL_STATUS),
        (1, {"private": PRIVATE_CANARY}, SelectedStartupFailure.ADD_ACKNOWLEDGEMENT),
        (
            2,
            {"cwd": "file:///fixture/work%20:@"},
            SelectedStartupFailure.EXECUTOR_SHELL,
        ),
        (
            2,
            {"shell": {"name": "bash", "path": "/usr/bin/bash"}},
            SelectedStartupFailure.EXECUTOR_SHELL,
        ),
        (
            2,
            {"shell": {"name": "bash", "path": "/bin/bash"}},
            SelectedStartupFailure.EXECUTOR_CWD,
        ),
        (
            2,
            {
                "shell": {"name": "bash", "path": "/bin/bash"},
                "cwd": "file://host/fixture/work%20:@",
            },
            SelectedStartupFailure.EXECUTOR_CWD,
        ),
        (
            2,
            {
                "shell": {"name": "bash", "path": "/bin/bash"},
                "cwd": "file:///%2Ffixture/work%20:@",
            },
            SelectedStartupFailure.EXECUTOR_CWD,
        ),
        (
            2,
            {
                "shell": {"name": "bash", "path": "/bin/bash"},
                "cwd": "file:///fixture/work%:@#tail",
            },
            SelectedStartupFailure.EXECUTOR_CWD,
        ),
        (
            2,
            {
                "shell": {"name": "bash", "path": "/bin/bash"},
                "cwd": "file:///fixture/other",
            },
            SelectedStartupFailure.EXECUTOR_CWD,
        ),
        (3, {"status": "pending"}, SelectedStartupFailure.EXECUTOR_STATUS),
        (
            3,
            {"status": "ready", "error": PRIVATE_CANARY},
            SelectedStartupFailure.EXECUTOR_STATUS,
        ),
    ],
    ids=[
        "local-present",
        "nonempty-add",
        "missing-shell",
        "different-shell-spelling",
        "missing-cwd",
        "uri-authority",
        "encoded-separator",
        "malformed-uri",
        "wrong-checkout",
        "not-ready",
        "ready-with-error",
    ],
)
@pytest.mark.asyncio
async def test_selected_startup_refuses_metadata_before_a_later_request(
    index: int, result: object, reason: SelectedStartupFailure
) -> None:
    exchanges = selected_exchanges()
    async with wire() as connection:
        startup = connection.start(
            prepare_selected_thread(
                connection.rpc, IDENTITY, SELECTED, BEARER, deadline=future_deadline()
            )
        )

        await initialize(connection)
        for offset, exchange in enumerate(exchanges[:index], 2):
            await answer(connection, offset, exchange)
        await answer(connection, index + 2, replace(exchanges[index], result=result))
        with pytest.raises(SelectedStartupError) as caught:
            await startup
        trailing = await remaining_bytes(connection)

        assert caught.value.reason is reason
        assert str(caught.value) == reason.value
        assert trailing == b""


@pytest.mark.parametrize(
    ("scope", "field", "value"),
    [
        ("thread", "id", BEARER),
        ("thread", "id", "018f0302-3491-4b42-92c0-9d15c2933393"),
        ("thread", "model", PRIVATE_CANARY),
        ("thread", "modelProvider", "openai"),
        ("thread", "cwd", "/fixture/other"),
        ("thread", "ephemeral", False),
        ("thread", "forkedFromId", THREAD_ID),
        ("thread", "parentThreadId", THREAD_ID),
        ("thread", "forkedFromId", OMITTED),
        ("thread", "parentThreadId", OMITTED),
        ("thread", "environments", []),
        ("thread", "environments", [{"environmentId": "local"}]),
        (
            "thread",
            "environments",
            [
                {
                    "environmentId": SELECTED.environment_id,
                    "cwd": str(SELECTED.workspace),
                    "runtimeWorkspaceRoots": ["/fixture/other"],
                },
            ],
        ),
        (
            "thread",
            "environments",
            [
                {
                    "environmentId": environment,
                    "cwd": str(SELECTED.workspace),
                    "runtimeWorkspaceRoots": [str(SELECTED.workspace)],
                }
                for environment in (SELECTED.environment_id, "another")
            ],
        ),
        ("thread", "turns", [{"id": PRIVATE_CANARY}]),
        ("response", "cwd", "/fixture/other"),
        ("response", "model", PRIVATE_CANARY),
        ("response", "runtimeWorkspaceRoots", []),
        ("response", "approvalPolicy", "on-request"),
    ],
    ids=[
        "bearer-as-id",
        "wrong-uuid-version",
        "nested-model",
        "nested-provider",
        "nested-cwd",
        "not-ephemeral",
        "fork",
        "parent",
        "missing-fork",
        "missing-parent",
        "no-selection",
        "local-selection",
        "nested-root",
        "extra-selection",
        "prior-turn",
        "response-cwd",
        "response-model",
        "response-roots",
        "response-approval",
    ],
)
@pytest.mark.asyncio
async def test_selected_startup_refuses_conflicting_thread_acknowledgements(
    scope: Literal["thread", "response"], field: str, value: object
) -> None:
    exchanges = selected_exchanges()
    payload = thread_fields()
    target = (
        payload if scope == "response" else cast("dict[str, object]", payload["thread"])
    )
    if value is OMITTED:
        del target[field]
    else:
        target[field] = value
    async with wire() as connection:
        startup = connection.start(
            prepare_selected_thread(
                connection.rpc, IDENTITY, SELECTED, BEARER, deadline=future_deadline()
            )
        )

        await initialize(connection)
        for index, exchange in enumerate(exchanges[:-1], 2):
            await answer(connection, index, exchange)
        await answer(connection, 6, replace(exchanges[-1], result=payload))
        with pytest.raises(SelectedStartupError) as caught:
            await startup
        trailing = await remaining_bytes(connection)

        assert caught.value.reason is SelectedStartupFailure.THREAD_ACKNOWLEDGEMENT
        assert trailing == b""


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://localhost:12345",
        "ws://127.1:12345",
        "ws://127.0.0.1:0",
        "ws://127.0.0.1:65536",
        "ws://127.0.0.1:12345/?private",
        "ws://127.0.0.1@host:12345",
    ],
    ids=[
        "hostname",
        "noncanonical-host",
        "zero-port",
        "large-port",
        "query",
        "userinfo",
    ],
)
@pytest.mark.asyncio
async def test_selected_startup_refuses_nonliteral_destinations_before_any_frame(
    endpoint: str,
) -> None:
    async with wire() as connection:
        with pytest.raises(SelectedStartupError) as caught:
            await prepare_selected_thread(
                connection.rpc,
                IDENTITY,
                replace(SELECTED, endpoint=endpoint),
                BEARER,
                deadline=future_deadline(),
            )
        trailing = await remaining_bytes(connection)

        assert caught.value.reason is SelectedStartupFailure.INVALID_EXPECTATION
        assert str(caught.value) == "selected_startup_invalid_expectation"
        assert trailing == b""


@pytest.mark.asyncio
async def test_selected_startup_refuses_invalid_bearer_before_any_frame() -> None:
    async with wire() as connection:
        bearer = "private-credential-canary"

        with pytest.raises(SelectedStartupError) as caught:
            await prepare_selected_thread(
                connection.rpc, IDENTITY, SELECTED, bearer, deadline=future_deadline()
            )
        trailing = await remaining_bytes(connection)

        assert caught.value.reason is SelectedStartupFailure.INVALID_BEARER
        assert str(caught.value) == "selected_startup_invalid_bearer"
        assert trailing == b""


@pytest.mark.parametrize("outcome", ["refusal", "eof", "deadline"])
@pytest.mark.asyncio
async def test_selected_startup_preserves_info_refusal_and_transport_abort(
    outcome: Literal["refusal", "eof", "deadline"],
) -> None:
    exchanges = selected_exchanges()
    deadline = future_deadline()
    observed_time = asyncio.get_running_loop().time()

    def clock() -> float:
        return observed_time

    async with wire(clock=clock) as connection:
        startup = connection.start(
            prepare_selected_thread(
                connection.rpc, IDENTITY, SELECTED, BEARER, deadline=deadline
            )
        )

        await initialize(connection)
        for index, exchange in enumerate(exchanges[:2], 2):
            await answer(connection, index, exchange)
        await receive(connection, 4, exchanges[2])
        if outcome == "refusal":
            await connection.send(
                frame({"id": 4, "error": {"code": -32603, "message": PRIVATE_CANARY}})
            )
            with pytest.raises(SelectedStartupError) as native:
                await startup
            reason = native.value.reason
            assert reason is SelectedStartupFailure.EXECUTOR_INFO_REFUSED
        else:
            if outcome == "eof":
                connection.peer_writer.write_eof()
            else:
                observed_time = deadline
                await connection.send(frame({"id": 4, "result": exchanges[2].result}))
            with pytest.raises(RpcError) as transport:
                await startup
            assert transport.value.reason is (
                RpcFailure.DISCONNECTED if outcome == "eof" else RpcFailure.DEADLINE
            )
        trailing = await remaining_bytes(connection)

        assert trailing == b""


@pytest.mark.parametrize("phase", ["initialize", "local-status"])
@pytest.mark.asyncio
async def test_selected_startup_refuses_premature_native_turn_completion(
    phase: Literal["initialize", "local-status"],
) -> None:
    exchanges = selected_exchanges()
    async with wire() as connection:
        startup = connection.start(
            prepare_selected_thread(
                connection.rpc, IDENTITY, SELECTED, BEARER, deadline=future_deadline()
            )
        )

        if phase == "initialize":
            await connection.peer_reader.readline()
        else:
            await initialize(connection)
            await receive(connection, 2, exchanges[0])
        await connection.send(frame({"method": "turn/completed", "params": {}}))
        with pytest.raises(TurnError) as caught:
            await startup
        trailing = await remaining_bytes(connection)

        assert caught.value.reason is TurnFailure.PREMATURE_COMPLETION
        assert trailing == b""
