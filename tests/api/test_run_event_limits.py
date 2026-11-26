import base64
import json
import os
from hashlib import sha256
from typing import Literal

import httpx
import pytest
from sqlalchemy import insert, update

from kora.api.application import create_application
from kora.api.exchange import COMPONENT, ApiExchange
from kora.api.run_event_route import RunEventRoute
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import api_token, event
from kora.ir.json import parse_json_object
from kora.models.errors import ApiErrorEnvelope
from kora.models.run_events import (
    CURSOR_BYTES,
    PAGE_BYTES,
    RunEventPage,
    TokenEventActor,
)
from kora.observability import (
    Level,
    LoggingSettings,
    ProcessDiagnostics,
    configure_process,
)
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.support import RequestIds, api_capture, client_for
from tests.cli.test_deploy_command import kora
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger, reached
from tests.db.seed import Seeder
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["api_capture"]

PAYLOAD_CANARY = "private-finite-timeline-payload"
SELECTOR_CANARY = "private-finite-timeline-selector"
CORRELATION_CANARY = "private-finite-client-correlation"
VIEWER_CREDENTIAL = "private-finite-personal-bearer"
SERVICE_CREDENTIAL = "private-finite-service-bearer"
OTHER_CREDENTIAL = "private-finite-other-bearer"
ABSENT_RUN = "00000000000000000000000000"


def _app(database: Database, diagnostics: ProcessDiagnostics) -> ApiExchange:
    clock = AuthorityClock()
    return create_application(
        StaticTokenAuthenticator({}, database),
        diagnostics,
        clock,
        RequestIds(),
        run_events=RunEventRoute(database, clock),
    )


def _route(namespace: str, run_id: str) -> str:
    return f"/api/v1/namespaces/{namespace}/runs/run_{run_id}/events"


def _authorization(credential: str = VIEWER_CREDENTIAL) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


async def _token(
    database: Database,
    namespace_id: str,
    credential: str = VIEWER_CREDENTIAL,
    kind: Literal["user", "service"] = "user",
) -> None:
    async with database.transaction() as writer:
        token = await Seeder(writer).token(
            sha256(credential.encode()).hexdigest(), kind=kind
        )
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == token)
            .values(
                approve=False,
                namespaces=[{"namespace_id": namespace_id, "transcripts": False}],
                runtime="local" if kind == "service" else None,
            )
        )


async def _run(seed: Seeder, namespace_id: str, graph_name: str = "pipeline") -> str:
    graph = await seed.graph(namespace_id, graph_name)
    version = await seed.graph_version(graph)
    return await seed.run(namespace_id, graph, version)


async def _events(
    seed: Seeder,
    namespace_id: str,
    run: str,
    count: int,
    kind: str = "run.created",
    actor: dict[str, str] | None = None,
    node: str | None = None,
    attempt: str | None = None,
) -> list[str]:
    ids = [seed.new_id() for _ in range(count)]
    rows = [
        {
            "id": identity,
            "namespace_id": namespace_id,
            "run_id": run,
            "kind": kind,
            "at": seed.clock.now(),
            "actor": {"kind": "system", "component": "scheduler"}
            if actor is None
            else actor,
            "node_instance_id": node,
            "attempt_id": attempt,
            "payload": {"private": PAYLOAD_CANARY},
            **seed.stamps(),
        }
        for identity in ids
    ]
    await seed.connection.execute(insert(event), rows)
    return [f"event_{identity}" for identity in reversed(ids)]


def _cursor_with_namespace(cursor: str, namespace: str) -> str:
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    position = parse_json_object(json.loads(decoded))
    position["namespace"] = namespace
    encoded = json.dumps(position, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


@pytest.mark.asyncio
async def test_run_event_page_serves_maximum_legal_rows_with_one_lookahead(
    database: Database, api_capture: Capture
) -> None:
    kind = "\x1b" * 255
    actor = TokenEventActor(kind="user", owner='"' * 256, token_id="\\" * 256)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace()
        run = await _run(seed, namespace)
        node = await seed.node(run, "step")
        attempt = await seed.attempt(node)
        saved_actor = {
            "kind": actor.kind,
            "owner": actor.owner,
            "token_id": actor.token_id,
        }
        expected = await _events(
            seed, namespace, run, 101, kind, saved_actor, node, attempt
        )
    await _token(database, namespace)
    app = _app(database, api_capture.diagnostics)
    path = _route("default", run)
    headers = _authorization()

    async with client_for(app) as client:
        responses = [
            await client.get(path, params={"limit": limit}, headers=headers)
            for limit in (1, 50, 100)
        ]
        default = await client.get(path, headers=headers)
        maximum = RunEventPage.model_validate_json(responses[-1].content)
        continuation = await client.get(
            path, params={"cursor": maximum.next_cursor}, headers=headers
        )

    conservative_bound = 100 * 4_608 + 8_192 + 256
    assert CURSOR_BYTES == 8_192
    assert conservative_bound == 469_248 < PAGE_BYTES == 1_048_576
    for limit, response in zip((1, 50, 100), responses, strict=True):
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert len(response.content) <= limit * 4_608 + CURSOR_BYTES + 256
        assert PAYLOAD_CANARY not in response.text
        page = RunEventPage.model_validate_json(response.content)
        assert [row.id for row in page.items] == expected[:limit]
        assert page.next_cursor is not None
        assert len(page.next_cursor.encode("ascii")) <= CURSOR_BYTES
        for row in page.items:
            assert (row.kind, row.actor, row.run_id) == (kind, actor, f"run_{run}")
            assert (row.node_instance_id, row.attempt_id) == (
                f"node_{node}",
                f"att_{attempt}",
            )
            assert len(row.model_dump_json().encode()) <= 4_608
    assert default.status_code == 200
    assert RunEventPage.model_validate_json(default.content) == maximum
    assert continuation.status_code == 200
    last = RunEventPage.model_validate_json(continuation.content)
    assert [row.id for row in last.items] == expected[100:]
    assert last.next_cursor is None


@pytest.mark.asyncio
async def test_run_event_refusals_bind_cursor_scope_and_keep_diagnostics_private(
    database: Database, api_capture: Capture
) -> None:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        home = await seed.namespace()
        origin = await _run(seed, home)
        neighbor = await _run(seed, home, "neighbor")
        foreign = await seed.namespace("foreign")
        remote = await _run(seed, foreign)
        for namespace, run in ((home, origin), (home, neighbor), (foreign, remote)):
            await _events(seed, namespace, run, 2)
    await _token(database, home)
    await _token(database, home, SERVICE_CREDENTIAL, kind="service")
    await _token(database, foreign, OTHER_CREDENTIAL)
    app = _app(database, api_capture.diagnostics)
    source = _route("default", origin)
    target = _route("foreign", remote)
    headers = {**_authorization(), "X-Request-ID": CORRELATION_CANARY}
    other = _authorization(OTHER_CREDENTIAL)
    unsupported = f"follow=true&cursor={SELECTOR_CANARY}%21"

    async with client_for(app) as client:
        first = await client.get(source + "?limit=1", headers=headers)
        position = RunEventPage.model_validate_json(first.content).next_cursor
        assert position is not None
        cursor = f"cursor={position}"
        continuation = await client.get(
            source + f"?limit=1&follow=false&{cursor}", headers=headers
        )
        other_run = await client.get(_route("default", neighbor), headers=headers)
        other_namespace = await client.get(target, headers=other)
        invalid_paths = [
            source + f"?cursor={SELECTOR_CANARY}%21",
            source + "?cursor=e30",
            source + "?cursor=" + "A" * (CURSOR_BYTES + 1),
            source + f"?limit=1&cursor={_cursor_with_namespace(position, foreign)}",
            source + f"?limit=2&{cursor}",
            source + "?limit=101",
            source + "?limit=1&limit=1",
            source + "?cursor=",
            source + f"?{SELECTOR_CANARY}=1",
            source + "?follow=true",
            _route("default", neighbor) + f"?limit=1&{cursor}",
        ]
        invalid = [await client.get(path, headers=headers) for path in invalid_paths]
        invalid.append(await client.get(target + f"?limit=1&{cursor}", headers=other))
        missing = await client.get(
            _route("default", ABSENT_RUN) + "?" + unsupported, headers=headers
        )
        outside = await client.get(
            _route("default", remote) + "?" + unsupported, headers=headers
        )
        denied_scope = await client.get(target + "?" + unsupported, headers=headers)
        service = await client.get(
            source + "?" + unsupported, headers=_authorization(SERVICE_CREDENTIAL)
        )
        cookie_only = await client.get(
            source + "?" + unsupported,
            headers={"Cookie": f"kora_token={VIEWER_CREDENTIAL}"},
        )
        duplicate = await client.get(
            source + "?" + unsupported,
            headers=[
                ("Authorization", headers["Authorization"]),
                ("Authorization", "Bearer " + SERVICE_CREDENTIAL),
            ],
        )

    assert first.status_code == continuation.status_code == 200
    assert other_run.status_code == other_namespace.status_code == 200
    [latest] = RunEventPage.model_validate_json(first.content).items
    [next_event] = RunEventPage.model_validate_json(continuation.content).items
    assert next_event.id < latest.id
    refusals = [
        *(
            (response, 422, "API003", "Request validation failed.")
            for response in invalid
        ),
        (missing, 404, "API001", "Object not found."),
        (outside, 404, "API001", "Object not found."),
        (denied_scope, 403, "API005", "Operation is forbidden."),
        (service, 403, "API005", "Operation is forbidden."),
        (cookie_only, 401, "API009", "Authentication is required."),
        (duplicate, 401, "API009", "Authentication is required."),
    ]
    records = api_capture.finish()
    for response, status, code, message in refusals:
        _assert_fixed_refusal(response, status, code, message)
        identity = response.headers["x-request-id"]
        [completed] = [
            row
            for row in records
            if row["event"] == "api.request.completed" and row["request_id"] == identity
        ]
        assert completed["status"] == status
        assert completed["code"] == code
    assert (
        cookie_only.headers["www-authenticate"]
        == duplicate.headers["www-authenticate"]
        == "Bearer"
    )
    [finished] = [
        row
        for row in records
        if row["event"] == "api.run_events.finished"
        and row["request_id"] == invalid[0].headers["x-request-id"]
    ]
    assert (finished["code"], finished["rows"], finished["bytes"]) == ("API003", 0, 0)
    encoded = json.dumps(records) + "".join(response.text for response, *_ in refusals)
    for secret in (
        VIEWER_CREDENTIAL,
        SERVICE_CREDENTIAL,
        OTHER_CREDENTIAL,
        PAYLOAD_CANARY,
        SELECTOR_CANARY,
        CORRELATION_CANARY,
        position,
    ):
        assert secret not in encoded


def _assert_fixed_refusal(
    response: httpx.Response, status: int, code: str, message: str
) -> None:
    error = ApiErrorEnvelope.model_validate_json(response.content).error
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-request-id"] == error.request_id
    assert error.request_id.startswith("req_api_")
    assert error.code == code
    assert error.message == message
    assert not error.retryable
    assert error.details.model_dump() == {}


@pytest.mark.asyncio
async def test_human_cli_escapes_saved_run_event_controls(
    database: Database, api_capture: Capture
) -> None:
    kind = "phase\r\n\x1b[\u202e"
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace()
        run = await _run(seed, namespace)
        [identity] = await _events(seed, namespace, run, 1, kind)
    await _token(database, namespace)
    app = _app(database, api_capture.diagnostics)

    async with running_app(app) as origin:
        result = await kora(
            ["events", f"run_{run}"],
            {
                "KORA_URL": origin,
                "KORA_TOKEN": VIEWER_CREDENTIAL,
                "KORA_NAMESPACE": "default",
            },
        )

    assert result.code == 0
    stdout = result.stdout.decode("utf-8")
    diagnostics = result.stderr.decode("utf-8")
    [line] = stdout.splitlines()
    assert line.endswith(f" {identity} phase\\r\\n\\x1b[\\u202e scheduler")
    assert "\x1b" not in stdout
    assert "\u202e" not in stdout
    assert "\r" not in stdout
    assert PAYLOAD_CANARY not in stdout + diagnostics
    assert VIEWER_CREDENTIAL not in stdout + diagnostics


@pytest.mark.asyncio
async def test_run_event_response_survives_a_failed_diagnostic_sink(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace()
        run = await _run(seed, namespace)
        [identity] = await _events(seed, namespace, run, 1)
    await _token(database, namespace)
    ledger = ConnectionLedger(database)
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    clock = AuthorityClock()

    try:
        diagnostics = configure_process(
            COMPONENT,
            LoggingSettings(level=Level.DEBUG),
            "api-run-event-sink",
            clock.now,
            write_fd,
        )
        try:
            assert await reached(lambda: diagnostics.snapshot().sink_failed)
            app = _app(database, diagnostics)
            path = _route("default", run)
            async with client_for(app) as client:
                finite = await client.get(path, headers=_authorization())
                refused = await client.get(
                    path + "?follow=true", headers=_authorization()
                )
        finally:
            diagnostics.shutdown(1)
    finally:
        os.close(write_fd)

    assert finite.status_code == 200
    [row] = RunEventPage.model_validate_json(finite.content).items
    assert row.id == identity
    _assert_fixed_refusal(refused, 422, "API003", "Request validation failed.")
    assert ledger.outstanding == database.units_in_flight() == 0
    assert diagnostics.snapshot().sink_failed
    assert not diagnostics.writer_is_alive()
