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
from sqlalchemy import Engine, insert, update
from sqlalchemy import event as sql_events
from sqlalchemy.util import await_
from starlette.types import ASGIApp, Message

from kora.api.application import create_application
from kora.api.run_event_route import RunEventRoute
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database, DatabaseSettings, Lifecycle
from kora.db.tables import api_token, event
from kora.db.tokens import REVOKED, revoke_token
from kora.engine.run_settlement import RunSettlementState, settle_run
from kora.ir.json import JsonObject, parse_json_object
from kora.models.run_events import RunEventPage
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import AuthenticationContext, Principal, RequestInfo
from tests.api.support import HttpCall, RequestIds, api_capture, client_for
from tests.cli.test_deploy_command import kora
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger, WriterCutoffLoop, until
from tests.db.seed import Seeder
from tests.db.test_run_event_reads import SYSTEM, seed_event, seed_run
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


class ObservedCurrentAuthenticator(StaticTokenAuthenticator):
    def __init__(self, database: Database) -> None:
        super().__init__({}, database)
        self.current: list[bool] = []

    async def authenticate(
        self, request: RequestInfo, ctx: AuthenticationContext
    ) -> Principal:
        self.current.append(request.requires_current_authority)
        return await super().authenticate(request, ctx)


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
        await revoke_token(writer, actor)
    authentication = ObservedCurrentAuthenticator(database)
    app = create_application(
        authentication,
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
    assert authentication.current == [True] * 5
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


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["node", "attempt", "outside", "actor"])
async def test_run_event_projection_enforces_exact_owner_without_partial_page(
    database: Database,
    api_capture: Capture,
    corruption: str,
) -> None:
    async with database.transaction() as writer:
        scope = await seed_run(writer)
        good = await seed_event(writer, scope, SYSTEM, 128)
        other = await seed_run(writer, "foreign")
        bad = await seed_event(writer, scope, SYSTEM, 128, event_id="7" + "Z" * 25)
        replacements: dict[str, dict[str, object]] = {
            "node": {"node_instance_id": other.node_id},
            "attempt": {"attempt_id": other.attempt_id},
            "outside": {"namespace_id": other.namespace_id},
            "actor": {"actor": {"kind": "system", "component": "wrong"}},
        }
        replacement = replacements[corruption]
        await writer.execute(
            update(event).where(event.c.id == bad).values(**replacement)
        )
    await _token(database, scope.namespace_id)
    app, path = observed_events(database, api_capture, scope.run_id)

    async with client_for(app) as client:
        page = await client.get(path, params={"limit": 1}, headers=_authorization())

    if corruption == "outside":
        assert page.status_code == 200
        assert page.json()["items"][0]["id"] == "event_" + good
        assert page.json()["next_cursor"] is None
    else:
        assert page.status_code == 500
        assert page.json()["error"]["code"] == "API010"
        assert set(page.json()) == {"error"}


def observed_events(
    database: Database, capture: Capture, run_id: str
) -> tuple[ASGIApp, str]:
    clock = AuthorityClock()
    return create_application(
        StaticTokenAuthenticator({}, database),
        capture.diagnostics,
        clock,
        RequestIds(),
        run_events=RunEventRoute(database, clock),
    ), f"/api/v1/namespaces/default/runs/run_{run_id}/events"


@dataclass
class DisconnectableEventPage(HttpCall):
    dropped: asyncio.Event = field(default_factory=asyncio.Event)

    async def receive(self) -> Message:
        await self.dropped.wait()
        return {"type": "http.disconnect"}


class NativePageHold:
    def __init__(self, engine: Engine) -> None:
        self.engine: Engine = engine
        self.entered: asyncio.Event = asyncio.Event()
        self.release_read: asyncio.Event = asyncio.Event()
        self.cleanup: asyncio.Event = asyncio.Event()
        self.release_cleanup: asyncio.Event = asyncio.Event()
        self.page_task: asyncio.Task[object] | None = None
        self.reader: object | None = None

    def after_projection(
        self, connection: object, cursor: object, statement: str, *details: object
    ) -> None:
        if "ORDER BY selected.id DESC" in statement and not self.entered.is_set():
            self.page_task = asyncio.current_task()
            self.reader = connection
            self.entered.set()
            await_(self.release_read.wait())

    def before_rollback(self, connection: object) -> None:
        if connection is self.reader and not self.cleanup.is_set():
            self.cleanup.set()
            await_(self.release_cleanup.wait())

    def attach(self) -> None:
        sql_events.listen(self.engine, "after_cursor_execute", self.after_projection)
        sql_events.listen(self.engine, "rollback", self.before_rollback)

    def detach(self) -> None:
        self.release_read.set()
        self.release_cleanup.set()
        sql_events.remove(self.engine, "after_cursor_execute", self.after_projection)
        sql_events.remove(self.engine, "rollback", self.before_rollback)


def test_run_event_native_deadline_is_fixed_budget_refusal(
    database_settings: DatabaseSettings,
    api_capture: Capture,
) -> None:
    loop = WriterCutoffLoop()

    async def exercise() -> None:
        database = Database(database_settings)
        try:
            case = await _settled(database)
            await _token(database, case.scope.namespace_id)
            app, path = observed_events(database, api_capture, case.run_id)
            ledger = ConnectionLedger(database)
            hold = NativePageHold(database.reader_engine.sync_engine)
            hold.release_cleanup.set()
            hold.attach()
            async with client_for(app) as client:
                request = asyncio.create_task(
                    client.get(path, headers=_authorization())
                )
                try:
                    await asyncio.wait_for(hold.entered.wait(), 10)
                    loop.advance_to(loop.time() + 5)
                    response = await asyncio.wait_for(request, 10)
                finally:
                    hold.detach()
                resumed = await client.get(path, headers=_authorization())

            assert response.status_code == 409
            assert response.json()["error"] | {"request_id": None} == {
                "code": "API002",
                "message": "Reduce the page limit and retry.",
                "retryable": False,
                "details": {
                    "reason": "read_budget_exceeded",
                    "run_id": "run_" + case.run_id,
                },
                "request_id": None,
                "location": None,
            }
            assert hold.cleanup.is_set()
            assert resumed.status_code == 200
            assert ledger.outstanding == database.units_in_flight() == 0
        finally:
            await database.close()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise())


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_run_event_native_disconnect_or_shutdown_settles_once(
    database: Database,
    api_capture: Capture,
    shutdown: bool,
) -> None:
    case = await _settled(database)
    await _token(database, case.scope.namespace_id)
    app, path = observed_events(database, api_capture, case.run_id)
    ledger = ConnectionLedger(database)
    hold = NativePageHold(database.reader_engine.sync_engine)
    held = DisconnectableEventPage(
        path,
        headers=[(b"authorization", _authorization()["Authorization"].encode())],
    )
    hold.attach()

    request = asyncio.create_task(held.run(app))
    closing: asyncio.Task[None] | None = None
    ordinary_status: int | None = None
    try:
        await asyncio.wait_for(hold.entered.wait(), 10)
        if shutdown:
            closing = asyncio.create_task(database.close(deadline_seconds=10))
            await until(lambda: database.lifecycle is Lifecycle.CLOSING)
            request.cancel()
        else:
            held.dropped.set()
        await asyncio.wait_for(hold.cleanup.wait(), 10)
        assert hold.page_task is not None
        if not shutdown:
            async with client_for(app) as client:
                ordinary_status = (
                    await client.get(path, headers=_authorization())
                ).status_code
        pending = not request.done() and (closing is None or not closing.done())
        cancellation_edges = hold.page_task.cancelling()
        native_owned = ledger.outstanding
    finally:
        hold.detach()
    if closing is not None:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 10)
        await asyncio.wait_for(closing, 10)
    else:
        await asyncio.wait_for(request, 10)

    assert pending
    assert cancellation_edges == 1
    assert native_owned == 1
    assert shutdown or ordinary_status == 200
    assert shutdown or held.status == 503
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_run_event_continuation_pins_ceiling_and_sees_lower_live_membership(
    database: Database,
    api_capture: Capture,
) -> None:
    async with database.transaction() as writer:
        scope = await seed_run(writer)
        older = await seed_event(writer, scope, SYSTEM, 16)
        newest = await seed_event(writer, scope, SYSTEM, 16)
    await _token(database, scope.namespace_id)
    app, path = observed_events(database, api_capture, scope.run_id)

    async with client_for(app) as client:
        first = await client.get(path, params={"limit": 1}, headers=_authorization())
        first_page = RunEventPage.model_validate_json(first.content)
        async with database.transaction() as writer:
            highest = await seed_event(
                writer, scope, SYSTEM, 16, event_id="7" + "Z" * 25
            )
            lowest = await seed_event(
                writer, scope, SYSTEM, 16, event_id="0" * 25 + "1"
            )
        second = await client.get(
            path,
            params={"limit": 1, "cursor": first_page.next_cursor},
            headers=_authorization(),
        )
        second_page = RunEventPage.model_validate_json(second.content)
        third = await client.get(
            path,
            params={"limit": 1, "cursor": second_page.next_cursor},
            headers=_authorization(),
        )
        third_page = RunEventPage.model_validate_json(third.content)
        fresh = await client.get(path, params={"limit": 1}, headers=_authorization())

    assert (
        first.status_code
        == second.status_code
        == third.status_code
        == fresh.status_code
        == 200
    )
    assert [page.items[0].id for page in (first_page, second_page, third_page)] == [
        f"event_{identity}" for identity in (newest, older, lowest)
    ]
    assert third_page.next_cursor is None
    assert fresh.json()["items"][0]["id"] == f"event_{highest}"
