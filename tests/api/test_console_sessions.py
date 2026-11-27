import asyncio
import socket
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from sqlalchemy import Connection, update
from sqlalchemy import event as sql_events
from sqlalchemy.util import await_
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kora.api.application import create_application
from kora.api.console_assets import ConsoleAssets
from kora.api.console_routes import CSRF, REDEMPTION, SESSION, ConsoleRoutes
from kora.api.console_sessions import (
    CAPACITY,
    SESSION_LIFETIME,
    ConsoleSessions,
    Credential,
)
from kora.api.graph_deployment_limits import MAX_REQUEST_BYTES
from kora.api.run_event_route import RunEventRoute
from kora.conformance.capture import Capture
from kora.db.connection import Database, DatabaseSettings
from kora.db.tables import api_token
from kora.db.tokens import REVOKED
from kora.ir.json import parse_json_object
from kora.models.console import LOGIN_LINK_PATH, LoginSelection
from kora.models.run_events import RunEventPage
from kora.plugins.authentication.static_token import (
    StaticTokenAuthenticator,
    credential_digest,
)
from kora.protocols.authentication import RequestInfo
from tests.api.support import RequestIds, api_capture
from tests.client.support import ReadyServer, write_tls_material
from tests.db.closing import ConnectionLedger, WriterCutoffLoop, until
from tests.db.seed import Seeder
from tests.db.test_run_event_reads import SYSTEM, USER, seed_event, seed_run
from tests.plugins.authentication.support import AuthorityClock
from tests.support.parity import (
    FRONTEND_AUTH_OPERATIONS,
    schema_operations,
    uncovered_operations,
)

__all__ = ["api_capture"]

ORIGIN = "https://127.0.0.1:8480"
LOGOUT = "/api/v1/auth/session"
VIEWER_CREDENTIAL = "private-console-personal-credential"
AUTHORIZATION = {"authorization": "Bearer " + VIEWER_CREDENTIAL}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


@dataclass
class ConsoleExercise:
    client: httpx.AsyncClient
    app: ASGIApp
    catalog: dict[str, object]
    selection: LoginSelection
    alternate_run: str
    clock: AuthorityClock
    token_id: str
    first: str

    @property
    def canonical_path(self) -> str:
        return f"/api/v1/namespaces/default/runs/{self.selection.run_id}/events"

    async def issue(self) -> str:
        response = await self.client.post(
            LOGIN_LINK_PATH, json=self.selection.model_dump(), headers=AUTHORIZATION
        )
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        value: object = response.json()["url"]
        assert isinstance(value, str)
        assert value.startswith(ORIGIN + LOGOUT + "/")
        return value

    async def redeem(self, url: str) -> httpx.Response:
        return await self.client.get(url, headers=REDEMPTION)


@asynccontextmanager
async def console_exercise(
    database: Database, api_capture: Capture
) -> AsyncGenerator[ConsoleExercise]:
    async with database.transaction() as connection:
        scope = await seed_run(connection)
        await seed_event(connection, scope, SYSTEM, 16)
        last = await seed_event(connection, scope, USER, 16, event_id="7" + "Z" * 25)
        alternate = await seed_run(connection, "foreign")
        seed = Seeder(connection)
        graph = await seed.graph(scope.namespace_id, "alternate")
        version = await seed.graph_version(graph)
        other_run = await seed.run(scope.namespace_id, graph, version)
        token_id = await seed.token(sha256(VIEWER_CREDENTIAL.encode()).hexdigest())
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == token_id)
            .values(
                namespaces=[{"namespace_id": scope.namespace_id, "transcripts": False}]
            )
        )
    assert alternate.namespace_id != scope.namespace_id
    clock = AuthorityClock()
    authenticator = StaticTokenAuthenticator({}, database)
    events = RunEventRoute(database, clock)
    routes = ConsoleRoutes(
        ORIGIN,
        ConsoleAssets(b"document", {"assets/local.js": ("text/javascript", b"asset")}),
        ConsoleSessions(clock),
        credential_digest,
        authenticator.authenticate_digest,
        events,
    )
    app = create_application(
        authenticator,
        api_capture.diagnostics,
        clock,
        RequestIds(),
        run_events=events,
        console=routes,
    )
    framework = app.application
    assert isinstance(framework, FastAPI)
    paths: dict[str, object] = framework.openapi()["paths"]
    async with (
        framework.router.lifespan_context(framework),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, trust_env=False
        ) as client,
    ):
        yield ConsoleExercise(
            client,
            app,
            paths,
            LoginSelection(namespace="default", run_id="run_" + scope.run_id),
            "run_" + other_run,
            clock,
            token_id,
            "event_" + last,
        )
    assert routes.sessions.closed
    assert not routes.sessions.sessions
    assert not routes.sessions.reserved
    assert not routes.sessions.links


@pytest_asyncio.fixture
async def console(
    database: Database, api_capture: Capture
) -> AsyncGenerator[ConsoleExercise]:
    async with console_exercise(database, api_capture) as exercise:
        yield exercise


@pytest.mark.asyncio
async def test_console_login_link_enforces_raw_body_bound(
    console: ConsoleExercise,
) -> None:
    exact = console.selection.model_dump_json().encode().ljust(1024, b" ")
    headers = {**AUTHORIZATION, "content-type": "application/json"}

    accepted = await console.client.post(
        LOGIN_LINK_PATH, content=exact, headers=headers
    )
    refused = await console.client.post(
        LOGIN_LINK_PATH, content=exact + b" ", headers=headers
    )

    assert accepted.status_code == 201
    assert refused.json()["error"]["code"] == "API003"


@pytest.mark.asyncio
async def test_console_link_redeems_once_and_expires_at_sixty_seconds(
    console: ConsoleExercise,
) -> None:
    expired = await console.issue()
    console.clock.moment += timedelta(seconds=60)
    fresh = await console.issue()
    denied_metadata = {**REDEMPTION, "sec-fetch-site": "same-site"}
    rejected = await console.client.get(fresh, headers=denied_metadata)

    late = await console.redeem(expired)
    duplicate = await asyncio.gather(console.redeem(fresh), console.redeem(fresh))

    assert rejected.status_code == 403
    assert late.status_code == 401
    assert sorted(response.status_code for response in duplicate) == [303, 401]
    [redirect] = [response for response in duplicate if response.is_redirect]
    cookies = redirect.headers.get_list("set-cookie")
    assert (
        redirect.headers["location"]
        == "/console/runs/default/" + console.selection.run_id
    )
    assert redirect.headers["referrer-policy"] == "no-referrer"
    assert len(cookies) == 2
    assert all(
        "Secure" in cookie and "Path=/" in cookie and "SameSite=strict" in cookie
        for cookie in cookies
    )
    assert [
        cookie.startswith(SESSION + "=") for cookie in cookies if "HttpOnly" in cookie
    ] == [True]


@pytest.mark.asyncio
async def test_console_redemption_rechecks_and_consumes_original_credential(
    console: ConsoleExercise, database: Database
) -> None:
    login = await console.issue()
    async with database.transaction() as connection:
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == console.token_id)
            .values(expires_at=REVOKED)
        )

    refused = await console.redeem(login)
    async with database.transaction() as connection:
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == console.token_id)
            .values(expires_at=None)
        )
    replay = await console.redeem(login)
    fresh = await console.redeem(await console.issue())

    assert refused.json()["error"]["code"] == "API009"
    assert replay.json()["error"]["code"] == "API009"
    assert not refused.headers.get_list("set-cookie")
    assert not replay.headers.get_list("set-cookie")
    assert fresh.status_code == 303


@pytest.mark.asyncio
async def test_console_redemption_refuses_incomplete_body_without_reservation(
    console: ConsoleExercise,
) -> None:
    link = await console.issue()

    async def incomplete_body() -> AsyncGenerator[bytes]:
        await asyncio.Future[None]()
        yield b"body"

    async with asyncio.timeout(10):
        refused = await console.client.request(
            "GET",
            link,
            headers={**REDEMPTION, "content-length": "4"},
            content=incomplete_body(),
        )
        redeemed = await console.redeem(link)

    assert refused.json()["error"]["code"] == "API003"
    assert redeemed.status_code == 303


@pytest.mark.asyncio
async def test_console_redemption_cancels_during_current_token_read(
    console: ConsoleExercise, database: Database
) -> None:
    login = await console.issue()
    entered, release = asyncio.Event(), asyncio.Event()
    engine = database.reader_engine.sync_engine
    ledger = ConnectionLedger(database)

    def hold_read(
        _connection: Connection, _cursor: object, statement: str, *_unused: object
    ) -> None:
        if "FROM api_token" in statement and "api_token.digest" in statement:
            entered.set()
            await_(release.wait())

    sql_events.listen(engine, "before_cursor_execute", hold_read)
    pending = asyncio.create_task(console.redeem(login))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        sql_events.remove(engine, "before_cursor_execute", hold_read)
    replay = await console.redeem(login)

    assert replay.json()["error"]["code"] == "API009"
    assert not console.client.cookies
    assert ledger.outstanding == 0


@asynccontextmanager
async def native_console_listener(
    app: ASGIApp, directory: Path
) -> AsyncGenerator[tuple[int, ssl.SSLContext]]:
    tls = write_tls_material(directory)
    trust = ssl.create_default_context(cafile=str(tls.authority))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        server = ReadyServer(
            uvicorn.Config(
                app,
                log_config=None,
                access_log=False,
                interface="asgi3",
                http="h11",
                lifespan="off",
                ssl_certfile=str(tls.certificate),
                ssl_keyfile=str(tls.key),
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(server.ready.wait(), 10)
            yield listener.getsockname()[1], trust
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["link", "events", "oversized-events"])
async def test_console_h11_disconnect_releases_pending_current_authority(
    console: ConsoleExercise, database: Database, tmp_path: Path, surface: str
) -> None:
    login = await console.issue()
    headers = {"host": "127.0.0.1:8480", **REDEMPTION}
    path = httpx.URL(login).raw_path.decode("ascii")
    oversized = surface == "oversized-events"
    if surface != "link":
        assert (await console.redeem(login)).status_code == 303
        path = "/console" + console.canonical_path
        headers = {
            "host": "127.0.0.1:8480",
            "cookie": f"{SESSION}={console.client.cookies[SESSION]}",
            **SAME_ORIGIN,
        }
        if oversized:
            headers["content-length"] = str(MAX_REQUEST_BYTES + 2)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    overflow_received = asyncio.Event()
    engine = database.reader_engine.sync_engine
    ledger = ConnectionLedger(database)

    async def observe_ingress(scope: Scope, receive: Receive, send: Send) -> None:
        received = 0

        async def record_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BYTES:
                    overflow_received.set()
            return message

        await console.app(scope, record_receive, send)

    def hold_read(
        _connection: Connection, _cursor: object, statement: str, *_unused: object
    ) -> None:
        if "FROM api_token" in statement and "api_token.digest" in statement:
            entered.set()
            try:
                await_(release.wait())
            except asyncio.CancelledError:
                cancelled.set()
                raise

    lines = [
        f"GET {path} HTTP/1.1",
        *(f"{key}: {value}" for key, value in headers.items()),
    ]
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    async with native_console_listener(observe_ingress, tmp_path) as (port, trust):
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=trust)
        sql_events.listen(engine, "before_cursor_execute", hold_read)
        try:
            async with asyncio.timeout(10):
                writer.write(request)
                await writer.drain()
                await entered.wait()
                if oversized:
                    chunk = b"x" * 65_536
                    for _ in range(MAX_REQUEST_BYTES // len(chunk)):
                        writer.write(chunk)
                        await writer.drain()
                    writer.write(b"x")
                    await writer.drain()
                    await overflow_received.wait()
                writer.transport.abort()
                await cancelled.wait()
                await until(lambda: ledger.outstanding == 0)
        finally:
            writer.close()
            await writer.wait_closed()
            release.set()
            try:
                await asyncio.wait_for(until(lambda: ledger.outstanding == 0), 10)
            finally:
                sql_events.remove(engine, "before_cursor_execute", hold_read)
        response = (
            await console.redeem(login)
            if surface == "link"
            else await console.client.get(path, headers=SAME_ORIGIN)
        )

    assert cancelled.is_set()
    assert ledger.outstanding == 0
    assert response.status_code == (401 if surface == "link" else 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("in_scope", [False, True])
async def test_console_event_size_refusal_follows_scope(
    console: ConsoleExercise, in_scope: bool
) -> None:
    assert (await console.redeem(await console.issue())).status_code == 303
    path = "/console" + console.canonical_path
    if not in_scope:
        missing = "run_7" + "Z" * 25
        path = path.replace(console.selection.run_id, missing)
    body = b"x" * (MAX_REQUEST_BYTES + 1)

    response = await console.client.request(
        "GET", path, headers=SAME_ORIGIN, content=body
    )

    assert response.status_code == (422 if in_scope else 404)
    assert response.json()["error"]["code"] == ("API003" if in_scope else "API001")


def test_console_event_page_budget_starts_after_current_authority(
    database_settings: DatabaseSettings, api_capture: Capture
) -> None:
    loop = WriterCutoffLoop()

    async def exercise() -> None:
        database = Database(database_settings)
        try:
            async with console_exercise(database, api_capture) as active:
                assert (await active.redeem(await active.issue())).status_code == 303
                admitted, release = asyncio.Event(), asyncio.Event()
                engine = database.reader_engine.sync_engine

                def hold_read(
                    _connection: Connection,
                    _cursor: object,
                    statement: str,
                    *_unused: object,
                ) -> None:
                    if (
                        "FROM api_token" in statement
                        and "api_token.digest" in statement
                    ):
                        admitted.set()
                        await_(release.wait())

                sql_events.listen(engine, "before_cursor_execute", hold_read)
                request = asyncio.create_task(
                    active.client.get(
                        "/console" + active.canonical_path, headers=SAME_ORIGIN
                    )
                )
                try:
                    async with asyncio.timeout(10):
                        await admitted.wait()
                        loop.advance_to(loop.time() + 5)
                        release.set()
                        response = await request
                finally:
                    release.set()
                    await asyncio.gather(request, return_exceptions=True)
                    sql_events.remove(engine, "before_cursor_execute", hold_read)

                assert response.status_code == 200
                page = RunEventPage.model_validate_json(response.content)
                assert page.items[0].id == active.first
        finally:
            await database.close()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["expired", "revoked", "owner", "ungranted", "invalid_authority", "unavailable"],
)
async def test_console_session_reads_current_authority_on_each_page(
    console: ConsoleExercise, database: Database, change: str
) -> None:
    assert (await console.redeem(await console.issue())).is_redirect
    canonical = await console.client.get(
        console.canonical_path, headers=AUTHORIZATION, params={"limit": 1}
    )
    ingress = "/console" + console.canonical_path
    first = await console.client.get(ingress, headers=SAME_ORIGIN, params={"limit": 1})
    page = RunEventPage.model_validate_json(first.content)
    assert page.next_cursor is not None
    params = {"limit": "1", "cursor": page.next_cursor}
    wrong_run = await console.client.get(
        ingress.replace(console.selection.run_id, console.alternate_run),
        headers=SAME_ORIGIN,
        params=params,
    )
    wrong_namespace = await console.client.get(
        ingress.replace("/default/", "/foreign/"), headers=SAME_ORIGIN, params=params
    )
    values: dict[str, dict[str, object]] = {
        "expired": {"expires_at": console.clock.now()},
        "revoked": {"expires_at": REVOKED},
        "owner": {"owner": "different-owner"},
        "ungranted": {"namespaces": []},
        "invalid_authority": {"namespaces": [{}]},
    }
    if change == "unavailable":
        await database.close()
    else:
        field_values = values[change]
        async with database.transaction() as connection:
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == console.token_id)
                .values(field_values)
            )

    refused = await console.client.get(ingress, headers=SAME_ORIGIN, params=params)
    cookie_only = await console.client.get(console.canonical_path)

    expected = {
        "ungranted": "API005",
        "invalid_authority": "API010",
        "unavailable": "API011",
    }.get(change, "API009")
    assert refused.json()["error"]["code"] == expected
    if change != "unavailable":
        assert cookie_only.json()["error"]["code"] == "API009"
    assert first.headers["cache-control"] == "no-store"
    assert [event.id for event in page.items] == [console.first]
    assert page == RunEventPage.model_validate_json(canonical.content)
    assert wrong_run.json()["error"]["code"] == "API003"
    assert wrong_namespace.json()["error"]["code"] == "API005"


@pytest.mark.asyncio
async def test_console_session_signout_requires_origin_and_csrf(
    console: ConsoleExercise,
) -> None:
    assert (await console.redeem(await console.issue())).is_redirect
    csrf = console.client.cookies[CSRF]
    session = console.client.cookies[SESSION]
    verified = [("origin", ORIGIN), ("x-csrf-token", csrf)]
    forgeries = [
        [("origin", ORIGIN), ("x-csrf-token", "0" * 64)],
        [("origin", ORIGIN), ("x-csrf-token", csrf), ("x-csrf-token", csrf)],
        [("origin", ORIGIN + "0"), ("x-csrf-token", csrf)],
        [*verified, ("cookie", f"{SESSION}={session}; {CSRF}={csrf}; {CSRF}={csrf}")],
        [
            *verified,
            ("cookie", f"{SESSION}={session}; {SESSION}={session}; {CSRF}={csrf}"),
        ],
    ]

    refusals = [
        await console.client.delete(LOGOUT, headers=headers) for headers in forgeries
    ]
    success = await console.client.delete(LOGOUT, headers=verified)
    stale = await console.client.get(
        "/console" + console.canonical_path, headers=SAME_ORIGIN
    )

    assert [response.status_code for response in refusals] == [403, 403, 403, 403, 401]
    assert success.status_code == 204
    assert not console.client.cookies
    assert all("Max-Age=0" in value for value in success.headers.get_list("set-cookie"))
    assert stale.json()["error"]["code"] == "API009"


@pytest.mark.asyncio
async def test_console_ingress_is_exact_and_canonical_api_requires_bearer(
    console: ConsoleExercise,
    api_capture: Capture,
) -> None:
    known_page = f"/console/runs/default/{console.selection.run_id}"
    login = await console.issue()
    assert (await console.redeem(login)).is_redirect
    digest = credential_digest(RequestInfo("Bearer " + VIEWER_CREDENTIAL))
    assert digest is not None
    markers = (
        login,
        login.rsplit("/", 1)[1],
        digest,
        VIEWER_CREDENTIAL,
        console.client.cookies[SESSION],
        console.client.cookies[CSRF],
    )

    page = await console.client.get(known_page)
    known_asset = await console.client.get("/assets/local.js")
    denied = await console.client.get(known_page, headers={"host": "127.0.0.1:8481"})
    missing = await console.client.get("/assets/missing.js")
    alias = await console.client.get("/assets/%6cocal.js")
    deferred = await console.client.post(LOGOUT)
    foreign = await console.client.get(
        "/console" + console.canonical_path, headers={"sec-fetch-site": "same-site"}
    )
    canonical = await console.client.get(console.canonical_path)

    assert page.headers["content-security-policy"].endswith("form-action 'none'")
    assert (page.content, known_asset.content) == (b"document", b"asset")
    assert [
        response.status_code
        for response in [denied, missing, alias, deferred, foreign, canonical]
    ] == [403, 404, 404, 405, 403, 401]
    assert deferred.headers["allow"] == "DELETE"
    assert canonical.json()["error"]["code"] == "API009"
    assert {
        (method, path)
        for method, path in schema_operations(console.catalog)
        if "/auth/" in path
    } == FRONTEND_AUTH_OPERATIONS
    assert all(
        not path.startswith("/console/") and not path.startswith("/assets/")
        for path in console.catalog
    )
    assert uncovered_operations(console.catalog, SOURCE_ROOT) == []
    methods = parse_json_object(console.catalog[LOGIN_LINK_PATH])
    operation = parse_json_object(methods["post"])
    responses = parse_json_object(operation["responses"])
    receipt = parse_json_object(responses["201"])
    assert receipt["content"] == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/LoginLinkReceipt"}
        }
    }
    records = api_capture.finish()
    assert all(marker.encode() not in api_capture.reader.data for marker in markers)
    for response, route in (
        (page, "/console/runs/{ns}/{run}"),
        (known_asset, "/assets/{asset}"),
        (foreign, "/console/api/v1/namespaces/{ns}/runs/{run}/events"),
    ):
        [record] = [
            record
            for record in records
            if record.get("event") == "api.request.completed"
            and record.get("request_id") == response.headers["x-request-id"]
        ]
        assert record["route"] == route


@pytest.mark.asyncio
async def test_console_session_capacity_and_shutdown_release() -> None:
    clock = AuthorityClock()
    sessions = ConsoleSessions(clock)
    selection = LoginSelection(namespace="default", run_id="run_" + "0" * 26)
    credential = Credential("digest", "identity", "owner")
    keys = [sessions.issue(selection, credential) for _ in range(CAPACITY)]
    first = keys[0]
    assert first is not None
    reserved = asyncio.Event()

    async def redeem() -> None:
        with sessions.reserve(first) as link:
            assert link is not None
            reserved.set()
            await asyncio.Future[None]()

    pending = asyncio.create_task(redeem())
    async with asyncio.timeout(10):
        await reserved.wait()
        assert sessions.issue(selection, credential) is None
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    with sessions.reserve(first) as consumed:
        next_code = sessions.issue(selection, credential)
    clock.moment += timedelta(seconds=60)
    reclaimed = sessions.issue(selection, credential)
    assert reclaimed is not None
    with sessions.reserve(reclaimed) as admitted:
        assert admitted is not None
        started = sessions.start(admitted, clock.now())
    assert started is not None
    for _ in range(CAPACITY - 1):
        code = sessions.issue(selection, credential)
        assert code is not None
        with sessions.reserve(code) as ready:
            assert ready is not None
            assert sessions.start(ready, clock.now()) is not None
    denied = sessions.issue(selection, credential)
    assert denied is not None
    with sessions.reserve(denied) as ready:
        assert ready is not None
        blocked = sessions.start(ready, clock.now())
    clock.moment += SESSION_LIFETIME
    expired = sessions.current(started[0])
    replacement = sessions.issue(selection, credential)
    assert replacement is not None
    with sessions.reserve(replacement) as ready:
        assert ready is not None
        recovered = sessions.start(ready, clock.now())
    sessions.close()

    assert blocked is None
    assert expired is None
    assert recovered is not None
    assert len(set(keys)) == CAPACITY
    assert consumed is None
    assert next_code is not None
    assert started is not None
    assert sessions.current(started[0]) is None
    assert not sessions.sessions
    assert not sessions.reserved
    assert not sessions.links
