import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import update

from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import api_token
from kora.db.tokens import REVOKED
from kora.ir.json import parse_json_object
from kora.models.errors import ApiErrorEnvelope
from tests.api.support import (
    POISON,
    HttpCall,
    api_capture,
    application,
    client_for,
    lifespan,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture"]


@pytest.mark.asyncio
async def test_health_probes_expose_only_fixed_status(
    database: Database, api_capture: Capture
) -> None:
    ledger = ConnectionLedger(database)
    app = application(database, api_capture)
    await database.close()
    headers = [
        [],
        [("Authorization", "Bearer " + POISON)],
        [("Authorization", "Basic " + POISON)],
        [("Cookie", POISON)],
        [("Authorization", POISON), ("Authorization", POISON)],
    ]

    async with lifespan(app), client_for(app) as client:
        responses = [await client.get("/healthz", headers=values) for values in headers]
    records = api_capture.finish()

    assert all(
        response.status_code == 200 and response.json() == {"status": "ok"}
        for response in responses
    )
    assert ledger.checkouts == ledger.outstanding == 0
    assert not any(
        record["event"] == "authentication.static_token.outcome" for record in records
    )
    assert len({response.headers["x-request-id"] for response in responses}) == len(
        responses
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    ["missing", "malformed", "unknown", "expired", "revoked", "cookie", "duplicate"],
)
async def test_unauthenticated_requests_return_api009(
    database: Database, api_capture: Capture, category: str
) -> None:
    case = await seed_case(
        database,
        category,
        expires_at=START - timedelta(seconds=1)
        if category == "expired"
        else REVOKED
        if category == "revoked"
        else None,
    )
    credential = case.request.authorization
    assert credential is not None
    values: dict[str, list[tuple[str, str]]] = {
        "missing": [],
        "malformed": [("Authorization", "Basic " + POISON)],
        "unknown": [("Authorization", "Bearer " + POISON)],
        "expired": [("Authorization", credential)],
        "revoked": [("Authorization", credential)],
        "cookie": [("Cookie", "authorization=" + credential)],
        "duplicate": [("Authorization", credential), ("Authorization", credential)],
    }
    app = application(database, api_capture)

    async with client_for(app) as client:
        response = await client.get("/api/v1/openapi.json", headers=values[category])
    body = ApiErrorEnvelope.model_validate_json(response.content)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "allow" not in response.headers
    assert body.error.code.value == "API009"
    assert body.error.request_id == response.headers["x-request-id"]
    assert POISON not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "service"])
async def test_schema_requires_personal_principal_and_accepts_empty_grants(
    database: Database, api_capture: Capture, kind: str
) -> None:
    case = await seed_case(database, kind, kind)
    async with database.transaction() as connection:
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == case.principal.token_id)
            .values(namespaces=[])
        )
    authorization = case.request.authorization
    assert authorization is not None
    app = application(database, api_capture)

    async with client_for(app) as client:
        response = await client.get(
            "/api/v1/openapi.json", headers={"Authorization": authorization}
        )

    assert response.status_code == (200 if kind == "user" else 403)
    if kind == "user":
        schema = parse_json_object(response.json())
        assert schema["openapi"] == "3.1.0"
        assert isinstance(schema["paths"], dict)
        assert set(schema["paths"]) == {"/healthz"}
    else:
        assert (
            ApiErrorEnvelope.model_validate_json(response.content).error.code.value
            == "API005"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("POST", 405),
        ("PUT", 405),
        ("PATCH", 405),
        ("DELETE", 405),
        ("HEAD", 405),
        ("OPTIONS", 405),
        ("TRACE", 405),
        ("CONNECT", 405),
    ],
)
async def test_method_refusal_uses_trusted_allow(
    database: Database, api_capture: Capture, method: str, expected: int
) -> None:
    app = application(database, api_capture)
    call = HttpCall(method=method, headers=[(b"allow", POISON.encode())])

    await call.run(app)
    headers = dict(call.messages[0]["headers"])

    assert call.status == expected
    assert headers.get(b"allow") == (b"GET" if expected == 405 else None)
    assert call.receives == 0
    assert POISON.encode() not in call.body


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["FROB", "get", "QUERY"])
@pytest.mark.parametrize("path", ["/healthz", "/missing"])
async def test_unimplemented_methods_use_the_selected_origin_policy(
    database: Database, api_capture: Capture, method: str, path: str
) -> None:
    app = application(database, api_capture)
    call = HttpCall(path, method)

    await call.run(app)
    body = ApiErrorEnvelope.model_validate_json(call.body)

    assert call.status == 501
    assert body.error.code.value == "API013"
    assert b"allow" not in dict(call.messages[0]["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "HEAD", "FROB", "get"])
@pytest.mark.parametrize("kind", ["missing", "user", "service"])
async def test_protected_schema_wrong_method_authenticates_first(
    database: Database, api_capture: Capture, method: str, kind: str
) -> None:
    case = None if kind == "missing" else await seed_case(database, kind, kind)
    headers = (
        []
        if case is None
        else [(b"authorization", str(case.request.authorization).encode())]
    )
    call = HttpCall("/api/v1/openapi.json", method, headers)
    app = application(database, api_capture)

    await call.run(app)

    expected = (
        401
        if kind == "missing"
        else 403
        if kind == "service"
        else 405
        if method in {"POST", "HEAD"}
        else 501
    )
    assert call.status == expected
    if kind != "user":
        assert b"allow" not in dict(call.messages[0]["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "FROB", "get"])
@pytest.mark.parametrize(
    "path",
    [
        "/missing",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/healthz/",
        "/api/v1/openapi.json/",
        "/",
    ],
)
async def test_unknown_path_is_content_free_without_scoped_lookup(
    database: Database, api_capture: Capture, method: str, path: str
) -> None:
    ledger = ConnectionLedger(database)
    app = application(database, api_capture)
    await database.close()
    call = HttpCall(
        path,
        method,
        [(b"authorization", b"Bearer " + POISON.encode())],
        query=POISON.encode(),
    )

    await call.run(app)
    headers = dict(call.messages[0]["headers"])

    assert call.status == (501 if method in {"FROB", "get"} else 404)
    assert b"allow" not in headers
    assert b"www-authenticate" not in headers
    assert POISON.encode() not in call.body
    assert call.receives == ledger.checkouts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "failed"])
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "FROB"])
async def test_authentication_dependency_failures_preserve_safe_errors(
    database: Database, api_capture: Capture, failure: str, method: str
) -> None:
    case = await seed_case(database, failure)
    if failure == "failed":
        async with database.transaction() as connection:
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == case.principal.token_id)
                .values(kind=POISON)
            )
    app = application(database, api_capture)
    if failure == "unavailable":
        await database.close()
    call = HttpCall(
        "/api/v1/openapi.json",
        method,
        [(b"authorization", str(case.request.authorization).encode())],
    )

    await call.run(app)
    records = api_capture.finish()

    assert call.status == (503 if failure == "unavailable" else 500)
    assert b"allow" not in dict(call.messages[0]["headers"])
    assert POISON not in json.dumps(records)
    assert POISON.encode() not in call.body
    if method != "HEAD":
        body = ApiErrorEnvelope.model_validate_json(call.body)
        assert body.error.code.value == (
            "API011" if failure == "unavailable" else "API010"
        )
        assert body.error.retryable is (failure == "unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["401", "403", "404", "405", "500", "503"])
async def test_head_refusals_have_no_body_or_json_framing(
    database: Database, api_capture: Capture, scenario: str
) -> None:
    case = await seed_case(
        database, scenario, "service" if scenario == "403" else "user"
    )
    if scenario == "500":
        async with database.transaction() as connection:
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == case.principal.token_id)
                .values(kind=POISON)
            )
    app = application(database, api_capture)
    if scenario == "503":
        await database.close()
    path = (
        "/missing"
        if scenario == "404"
        else "/healthz"
        if scenario == "405"
        else "/api/v1/openapi.json"
    )
    headers = (
        []
        if scenario == "401"
        else [(b"authorization", str(case.request.authorization).encode())]
    )
    call = HttpCall(path, "HEAD", headers)

    await call.run(app)
    values = dict(call.messages[0]["headers"])

    assert call.status == int(scenario)
    assert call.body == b""
    assert b"content-length" not in values
    assert b"transfer-encoding" not in values
    assert values[b"x-request-id"] == b"req_api_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/healthz", "/mounted/healthz"])
async def test_catalog_and_dispatch_preserve_root_path(
    database: Database, api_capture: Capture, path: str
) -> None:
    call = HttpCall(path, root_path="/mounted")
    app = application(database, api_capture)

    await call.run(app)

    assert call.status == 200
    assert json.loads(call.body) == {"status": "ok"}


def test_route_authorization_table_covers_every_operation(
    api_capture: Capture, database: Database
) -> None:
    app = application(database, api_capture)
    framework = app.application

    assert isinstance(framework, FastAPI)
    routes: list[tuple[str, set[str] | None]] = []
    for route in framework.routes:
        assert isinstance(route, APIRoute)
        routes.append((route.path, route.methods))

    assert routes == [("/healthz", {"GET"}), ("/api/v1/openapi.json", {"GET"})]
    assert framework.debug is False
    assert framework.router.redirect_slashes is False


def test_parity_client_side_operations(
    api_capture: Capture, database: Database
) -> None:
    framework = application(database, api_capture).application
    assert isinstance(framework, FastAPI)

    schema = parse_json_object(framework.openapi())

    assert isinstance(schema["paths"], dict)
    assert set(schema["paths"]) == {"/healthz"}
    assert "servers" not in schema
