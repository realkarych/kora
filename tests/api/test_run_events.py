import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert, update
from starlette.types import Message

from kora.api.application import create_application
from kora.api.run_event_route import RunEventRoute
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import api_token, event
from kora.db.tokens import REVOKED
from kora.engine.run_settlement import RunSettlementState, settle_run
from kora.ir.json import JsonObject, parse_json_object
from kora.models.run_events import RunEventPage
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.support import HttpCall, RequestIds, api_capture, client_for
from tests.cli.test_deploy_command import kora
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.promotion_support import PromotionCase
from tests.engine.settlement_support import finished_run, settlement_context
from tests.mcp.test_stdio_deploy import start
from tests.plugins.authentication.support import AuthorityClock
from tests.support.parity import uncovered_operations

__all__ = ["api_capture"]

VIEWER_CREDENTIAL = "run-events-personal-credential"
OWNERSHIP_CANARY = "private-run-event-payload"
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


def _authorization(token: str = VIEWER_CREDENTIAL) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


async def _token(
    database: Database,
    namespace_id: str,
    credential: str = VIEWER_CREDENTIAL,
    kind: str = "user",
) -> str:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        identity = seed.new_id()
        await writer.execute(
            insert(api_token).values(
                id=identity,
                digest=sha256(credential.encode()).hexdigest(),
                kind=kind,
                owner="saved-actor",
                approve=False,
                platform=False,
                expires_at=None,
                runtime=None if kind == "user" else "local",
                namespaces=[{"namespace_id": namespace_id, "transcripts": False}],
                **seed.stamps(),
            )
        )
        return identity


async def _settled(database: Database) -> PromotionCase:
    case = await finished_run(database)
    case.clock.advance(5)
    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case))
    assert result is RunSettlementState.SETTLED
    return case


@pytest.mark.asyncio
async def test_run_events_show_one_settled_outcome_across_real_clients(
    database: Database, api_capture: Capture
) -> None:
    case = await _settled(database)
    await _token(database, case.scope.namespace_id)
    clock = AuthorityClock()
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        clock,
        RequestIds(),
        run_events=RunEventRoute(database, clock),
    )
    run_id = "run_" + case.run_id
    framework = app.application
    assert isinstance(framework, FastAPI)

    async with (
        running_app(app) as origin,
        KoraClient("http://127.0.0.1", VIEWER_CREDENTIAL, asgi_app=app) as local,
        httpx.AsyncClient(base_url=origin, trust_env=False) as client,
    ):
        route = f"/api/v1/namespaces/default/runs/{run_id}/events"
        remote = await client.get(route, headers=_authorization())
        bounded = await local.list_run_events("default", run_id)
        cli = await kora(
            ["events", run_id, "--json"],
            {
                "KORA_URL": origin,
                "KORA_TOKEN": VIEWER_CREDENTIAL,
                "KORA_NAMESPACE": "default",
            },
        )
        refusal = await kora(
            ["events", run_id, "--json", "--follow"],
            {
                "KORA_URL": origin,
                "KORA_TOKEN": VIEWER_CREDENTIAL,
                "KORA_NAMESPACE": "default",
            },
        )
        session = await start(
            {"KORA_URL": origin, "KORA_MCP_TOKEN": VIEWER_CREDENTIAL}, "operating"
        )
        listed = await session.request("tools/list", {})
        arguments: JsonObject = {
            "mode": "events",
            "namespace": "default",
            "run_id": run_id,
        }
        read = await session.request(
            "tools/call", {"name": "kora_runs", "arguments": arguments}
        )
        code, _ = await session.close()

    schema = parse_json_object(framework.openapi())
    paths = cast("dict[str, object]", schema["paths"])
    live_path = "/api/v1/namespaces/{ns}/runs/{run}/events"
    operation = cast("dict[str, JsonObject]", paths[live_path])["get"]
    parameters = cast("list[JsonObject]", operation["parameters"])
    [follow] = [parameter for parameter in parameters if parameter["name"] == "follow"]
    follow_schema = cast("JsonObject", follow["schema"])
    assert follow_schema["const"] is follow_schema["default"] is False
    assert follow_schema["type"] == "boolean"
    assert live_path + "/{event}" not in paths
    assert uncovered_operations(paths, SOURCE_ROOT) == []
    tools = cast("list[JsonObject]", cast("JsonObject", listed["result"])["tools"])
    [runs] = [tool for tool in tools if tool["name"] == "kora_runs"]
    assert "follow" not in cast(
        "JsonObject", cast("JsonObject", runs["inputSchema"])["properties"]
    )
    assert remote.status_code == 200
    assert cli.code == 0
    assert (
        parse_json_object(json.loads(remote.content))
        == bounded.document
        == json.loads(cli.stdout)
    )
    assert (
        parse_json_object(json.loads(remote.content))
        == cast("JsonObject", read["result"])["structuredContent"]
    )
    assert refusal.code == 1
    assert json.loads(refusal.stdout)["error"]["code"] == "API003"
    assert code == 0
    page = RunEventPage.model_validate_json(remote.content)
    [settlement] = [row for row in page.items if row.kind == "run.succeeded"]
    _, _, saved = await case.snapshot(database)
    [event_id] = [row["id"] for row in saved if row["kind"] == "run.succeeded"]
    assert (settlement.id, settlement.run_id, settlement.at) == (
        f"event_{event_id}",
        run_id,
        case.clock.now(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "expired", "ungranted"])
async def test_run_event_cursor_requires_current_scope_before_interpretation(
    database: Database,
    api_capture: Capture,
    change: str,
) -> None:
    case = await _settled(database)
    clock = AuthorityClock()
    viewer = await _token(database, case.scope.namespace_id)
    actor = await _token(database, case.scope.namespace_id, "run-events-old-actor")
    async with database.transaction() as writer:
        seed = Seeder(writer)
        actor_event = seed.new_id()
        await writer.execute(
            insert(event).values(
                id=actor_event,
                namespace_id=case.scope.namespace_id,
                run_id=case.run_id,
                node_instance_id=None,
                attempt_id=None,
                kind="run.created",
                at=clock.now(),
                actor={"kind": "user", "owner": "saved-actor", "token_id": actor},
                payload={"private": OWNERSHIP_CANARY},
                **seed.stamps(),
            )
        )
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == actor)
            .values(expires_at=clock.now() - timedelta(seconds=1))
        )
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        clock,
        RequestIds(),
        run_events=RunEventRoute(database, clock),
    )
    path = f"/api/v1/namespaces/default/runs/run_{case.run_id}/events"

    async with client_for(app) as client:
        finite = await client.get(path, headers=_authorization())
        first = await client.get(path + "?limit=1", headers=_authorization())
        position = RunEventPage.model_validate_json(first.content).next_cursor
        assert position is not None
        continuation = await client.get(
            path, params={"limit": 1, "cursor": position}, headers=_authorization()
        )
        absent = await client.get(
            "/api/v1/namespaces/default/runs/run_00000000000000000000000000/events?follow=true",
            headers=_authorization(),
        )
        async with database.transaction() as writer:
            values: dict[str, object] = {"namespaces": list[JsonObject]()}
            if change == "revoked":
                values = {"expires_at": REVOKED}
            elif change == "expired":
                values = {"expires_at": clock.now() - timedelta(seconds=1)}
            await writer.execute(
                update(api_token).where(api_token.c.id == viewer).values(**values)
            )
        denied = await client.get(
            path, params={"cursor": position + "%!"}, headers=_authorization()
        )

    expected = "API005" if change == "ungranted" else "API009"
    assert denied.json()["error"]["code"] == expected
    assert absent.json()["error"]["code"] == "API001"
    assert first.status_code == continuation.status_code == finite.status_code == 200
    assert continuation.json()["items"][0]["id"] < first.json()["items"][0]["id"]
    assert OWNERSHIP_CANARY not in finite.text
    [historical] = [
        row for row in finite.json()["items"] if row["id"] == f"event_{actor_event}"
    ]
    assert historical["actor"] == {
        "kind": "user",
        "owner": "saved-actor",
        "token_id": actor,
    }


@dataclass
class HeldEventPage(HttpCall):
    sending: asyncio.Event = field(default_factory=asyncio.Event)

    async def receive(self) -> Message:
        await asyncio.Future[None]()
        raise AssertionError("the request was not cancelled")

    async def send(self, message: Message) -> None:
        await super().send(message)
        if message["type"] == "http.response.start":
            self.sending.set()
            await asyncio.Future[None]()


@pytest.mark.asyncio
async def test_run_event_delivery_holds_no_native_read(
    database: Database,
    api_capture: Capture,
) -> None:
    case = await _settled(database)
    await _token(database, case.scope.namespace_id)
    ledger = ConnectionLedger(database)
    clock = AuthorityClock()
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        clock,
        RequestIds(),
        run_events=RunEventRoute(database, clock),
    )
    path = f"/api/v1/namespaces/default/runs/run_{case.run_id}/events"
    held = HeldEventPage(
        path,
        headers=[(b"authorization", _authorization()["Authorization"].encode())],
    )

    request = asyncio.create_task(held.run(app))
    try:
        await asyncio.wait_for(held.sending.wait(), 10)
        outstanding = ledger.outstanding
        async with client_for(app) as client:
            ordinary = await client.get(path, headers=_authorization())
    finally:
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    assert held.status == ordinary.status_code == 200
    assert outstanding == ledger.outstanding == 0


@pytest.mark.asyncio
async def test_run_event_authentication_unavailability_precedes_query(
    database: Database,
    api_capture: Capture,
) -> None:
    case = await _settled(database)
    await _token(database, case.scope.namespace_id)
    clock = AuthorityClock()
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        clock,
        RequestIds(),
        run_events=RunEventRoute(database, clock),
    )
    await database.close()

    async with client_for(app) as client:
        response = await client.get(
            f"/api/v1/namespaces/default/runs/run_{case.run_id}/events?follow=true",
            headers=_authorization(),
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "API011"
