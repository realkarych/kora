import json
from dataclasses import asdict
from typing import cast

import httpx
import pytest
from sqlalchemy import func, select, update
from starlette.types import Message

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.cel_eval.binding import load_binding
from kora.cel_eval.thread import EvaluationThread, Settings
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import graph, graph_version, namespace
from kora.ir.json import JsonObject
from kora.models.errors import ApiErrorEnvelope
from kora.models.graphs import DeployResponse
from tests.api.deployment_cases import (
    ALLOW_AGENT_SECRETS,
    GRAPH,
    HARNESS,
    NAMESPACE,
    preparer,
    skeleton_ir,
    unresolved_approval,
    with_agent_secret,
)
from tests.api.support import (
    HttpCall,
    api_capture,
    application,
    client_for,
    deployment_services,
)
from tests.plugins.authentication.support import AuthorityClock, seed_case

__all__ = ["api_capture", "preparer"]

PATH = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/versions"
EMPTY_FILE: JsonObject = {
    "name": "check.sh",
    "sha256": "sha256:"
    + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "size": 0,
    "executable": True,
}


def body(ir: JsonObject | None = None, graph: str = GRAPH) -> JsonObject:
    return {"graph": graph, "ir": skeleton_ir() if ir is None else ir, "resources": []}


def with_command_file() -> JsonObject:
    ir = skeleton_ir()
    nodes = cast(
        "dict[str, dict[str, dict[str, object]]]",
        cast("JsonObject", ir["graph"])["nodes"],
    )
    nodes["gate"]["config"]["resources"] = [EMPTY_FILE]
    return ir


def with_prompt(prompt: str) -> JsonObject:
    ir = skeleton_ir()
    nodes = cast(
        "dict[str, dict[str, dict[str, object]]]",
        cast("JsonObject", ir["graph"])["nodes"],
    )
    nodes["agent"]["config"]["prompt"] = prompt
    return ir


async def rows(database: Database) -> tuple[int, int]:
    async with database.read_transaction() as reader:
        graphs = await reader.scalar(select(func.count()).select_from(graph))
        versions = await reader.scalar(select(func.count()).select_from(graph_version))
    return int(graphs or 0), int(versions or 0)


async def authorized(database: Database, kind: str = "user") -> dict[str, str]:
    case = await seed_case(database, NAMESPACE if kind == "user" else "workers", kind)
    return {"Authorization": str(case.request.authorization)}


def error_of(response: httpx.Response) -> ApiErrorEnvelope:
    return ApiErrorEnvelope.model_validate(response.json())


@pytest.mark.asyncio
async def test_deploy_route_publishes_and_reuses(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers = await authorized(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        first = await client.post(PATH, json=body(), headers=headers)
        again = await client.post(PATH, json=body(), headers=headers)

    published = DeployResponse.model_validate(first.json())
    reused = DeployResponse.model_validate(again.json())
    assert (first.status_code, again.status_code) == (201, 200)
    assert published.graph_version_id.startswith("gv_")
    assert (published.number, published.reused, published.warnings) == (1, False, ())
    assert reused == published.model_copy(update={"reused": True})
    assert await rows(database) == (1, 1)
    records = [
        record
        for record in api_capture.finish()
        if record["event"] == "api.request.completed"
    ]
    assert {str(record["route"]) for record in records} == {
        "/api/v1/namespaces/{ns}/graphs/{graph}/versions"
    }
    rendered = json.dumps(records)
    assert all(
        private not in rendered
        for private in (
            NAMESPACE,
            published.deployment_digest,
            headers["Authorization"],
        )
    )


@pytest.mark.asyncio
async def test_deploy_route_refuses_selector_mismatch(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers = await authorized(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    mismatches = (
        (f"/api/v1/namespaces/{NAMESPACE}/graphs/other/versions", body()),
        (PATH, body(graph="other")),
        (PATH, body(skeleton_ir(graph="other"))),
        (PATH, body(skeleton_ir(namespace="other"))),
    )

    async with client_for(app) as client:
        responses = [
            await client.post(path, json=payload, headers=headers)
            for path, payload in mismatches
        ]

    assert {response.status_code for response in responses} == {422}
    assert {error_of(response).error.code for response in responses} == {"API003"}
    assert await rows(database) == (0, 0)


@pytest.mark.asyncio
async def test_deploy_route_authorizes_before_body(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    user = await authorized(database)
    service = await authorized(database, "service")
    foreign = await seed_case(database, "other")
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    malformed = b"{not json"
    foreign_path = "/api/v1/namespaces/other/graphs/skeleton/versions"

    async with client_for(app) as client:
        anonymous = await client.post(PATH, content=malformed)
        worker = await client.post(PATH, content=malformed, headers=service)
        outside = await client.post(foreign_path, content=malformed, headers=user)
        missing = await client.post(
            "/api/v1/namespaces/absent/graphs/skeleton/versions",
            content=malformed,
            headers=user,
        )
        wrong_method = await client.get(PATH, headers=user)
        anonymous_method = await client.get(PATH)

    assert foreign.principal.namespaces
    assert (anonymous.status_code, error_of(anonymous).error.code) == (401, "API009")
    assert anonymous.headers["www-authenticate"] == "Bearer"
    for refused in (worker, outside, missing):
        assert (refused.status_code, error_of(refused).error.code) == (403, "API005")
    assert (wrong_method.status_code, wrong_method.headers["allow"]) == (405, "POST")
    assert anonymous_method.status_code == 401
    assert await rows(database) == (0, 0)


@pytest.mark.asyncio
async def test_deploy_route_returns_document_findings(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers = await authorized(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    oversized = json.dumps(body() | {"padding": "x" * (4 * 1024 * 1024)}).encode()
    requests: tuple[tuple[bytes, str | None], ...] = (
        (json.dumps(body(skeleton_ir(harness="claude"))).encode(), "IR018"),
        (json.dumps(body(with_command_file())).encode(), "IR028"),
        (b"{not json", None),
        (
            b'{"graph": "skeleton", "graph": "skeleton", "ir": {}, "resources": []}',
            None,
        ),
        (
            json.dumps(body())
            .replace('"resources": []', '"resources": [NaN]')
            .encode(),
            None,
        ),
        (json.dumps(body() | {"resources": [{}]}).encode(), None),
        (
            json.dumps(body(cast("JsonObject", {"graph": "not a document"}))).encode(),
            None,
        ),
        ("\udcff".encode("utf-8", "surrogateescape"), None),
        (
            json.dumps(body())
            .replace('"resources": []', '"resources": [1e400]')
            .encode(),
            None,
        ),
        (
            json.dumps(body(with_prompt("ж" * 700_000)), ensure_ascii=False).encode(),
            None,
        ),
        (oversized, None),
    )

    async with client_for(app) as client:
        responses = [
            await client.post(
                PATH,
                content=content,
                headers=headers | {"Content-Type": "application/json"},
            )
            for content, _ in requests
        ]

    for response, (_, code) in zip(responses, requests, strict=True):
        envelope = error_of(response)
        assert (response.status_code, envelope.error.code) == (422, "API003")
        details = envelope.model_dump(mode="json")["error"]["details"]
        if code is None:
            assert details == {}
        else:
            assert code in {error["code"] for error in details["errors"]}
    assert await rows(database) == (0, 0)


@pytest.mark.asyncio
async def test_deploy_route_reports_operations_that_could_not_complete(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers = await authorized(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        incomplete = await client.post(
            PATH, json=body(unresolved_approval()), headers=headers
        )
        async with preparer.hold():
            busy = await client.post(PATH, content=b"{not json", headers=headers)
        admitted = await client.post(PATH, json=body(), headers=headers)

    assert (incomplete.status_code, error_of(incomplete).error.code) == (500, "API010")
    assert error_of(incomplete).error.retryable is False
    assert (busy.status_code, error_of(busy).error.code) == (503, "API011")
    assert error_of(busy).error.retryable is True
    assert admitted.status_code == 201
    assert await rows(database) == (1, 1)


@pytest.mark.asyncio
async def test_deploy_route_reports_an_unavailable_evaluator(
    database: Database, api_capture: Capture
) -> None:
    headers = await authorized(database)
    owner = EvaluationThread(load_binding(), Settings(outstanding_bytes=1))

    async with DeploymentPreparer(AuthorityClock(), owner) as constrained:
        app = application(
            database, api_capture, deployment_services(database, constrained, HARNESS)
        )
        async with client_for(app) as client:
            response = await client.post(PATH, json=body(), headers=headers)

    assert (response.status_code, error_of(response).error.code) == (503, "API011")
    assert error_of(response).error.retryable is True
    assert await rows(database) == (0, 0)


class DisconnectingCall(HttpCall):
    async def receive(self) -> Message:
        self.receives += 1
        return {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_deploy_route_treats_a_disconnected_body_as_a_refusal(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers = await authorized(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    call = DisconnectingCall(
        path=PATH,
        method="POST",
        headers=[
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in headers.items()
        ],
    )

    await call.run(app)

    events = {str(record["event"]) for record in api_capture.finish()}
    assert call.status == 422
    assert ApiErrorEnvelope.model_validate_json(call.body).error.code == "API003"
    assert "api.request.failed" not in events
    assert await rows(database) == (0, 0)


def with_command_secret() -> JsonObject:
    ir = skeleton_ir()
    ir["secrets"] = {"aliases": {"API_TOKEN": "secret://env/API_TOKEN"}}
    nodes = cast(
        "dict[str, dict[str, dict[str, object]]]",
        cast("JsonObject", ir["graph"])["nodes"],
    )
    nodes["gate"]["config"]["secret_env"] = {"API_TOKEN": "API_TOKEN"}
    return ir


async def namespace_with_policy(
    database: Database, policy: JsonObject | None = None
) -> dict[str, str]:
    case = await seed_case(database, NAMESPACE)
    if policy is not None:
        async with database.transaction() as writer:
            await writer.execute(
                update(namespace)
                .where(namespace.c.id == case.principal.namespaces[0].namespace_id)
                .values(policy=policy)
            )
    return {"Authorization": str(case.request.authorization)}


@pytest.mark.asyncio
async def test_deploy_route_refuses_unenforced_agent_secrets(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers = await namespace_with_policy(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        refused = await client.post(
            PATH, json=body(with_agent_secret()), headers=headers
        )

    envelope = error_of(refused)
    errors = envelope.model_dump(mode="json")["error"]["details"]["errors"]
    assert (refused.status_code, envelope.error.code) == (422, "API003")
    assert [(error["code"], error["path"]) for error in errors] == [
        ("IR015", ["graph", "nodes", "agent", "config", "secret_env"])
    ]
    assert "API_TOKEN" not in refused.text
    assert await rows(database) == (0, 0)


@pytest.mark.asyncio
async def test_deploy_route_deploys_agent_secrets_the_namespace_allows(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers = await namespace_with_policy(database, asdict(ALLOW_AGENT_SECRETS))
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        allowed = await client.post(
            PATH, json=body(with_agent_secret()), headers=headers
        )

    assert allowed.status_code == 201
    assert await rows(database) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "ir", "status", "code", "paths"),
    [
        pytest.param(
            {"require_network_enforcement": True},
            with_command_secret(),
            422,
            "API003",
            [["graph", "nodes", "gate", "config", "secret_env"]],
            id="command-enforcement",
        ),
        pytest.param(
            {"allow_unenforced_agent_secrets": "true"},
            with_agent_secret(),
            500,
            "API010",
            [],
            id="invalid-policy",
        ),
    ],
)
async def test_deploy_route_refuses_against_the_saved_namespace_policy(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    policy: JsonObject,
    ir: JsonObject,
    status: int,
    code: str,
    paths: list[list[str]],
) -> None:
    headers = await namespace_with_policy(database, policy)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        refused = await client.post(PATH, json=body(ir), headers=headers)

    envelope = error_of(refused)
    details = envelope.model_dump(mode="json")["error"]["details"]
    assert (refused.status_code, envelope.error.code) == (status, code)
    assert [error["path"] for error in details.get("errors", [])] == paths
    assert await rows(database) == (0, 0)
