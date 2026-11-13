import json
from hashlib import sha256

import httpx
import pytest
from sqlalchemy import insert, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import api_token, graph_version, namespace
from kora.ir.agent_secrets import NamespaceSecretPolicy
from kora.ir.json import JsonObject
from kora.models.errors import ApiErrorEnvelope
from kora.models.runs import AcceptedRun, SkippedRun
from kora.protocols.authentication import Principal
from tests.api.deployment_cases import (
    ALLOW_AGENT_SECRETS,
    DEFAULT_POLICY,
    GRAPH,
    HARNESS,
    NAMESPACE,
    evaluator,
    preparer,
    publish,
    save_secret_policy,
    skeleton_ir,
    with_agent_secret,
)
from tests.api.support import api_capture, application, client_for, deployment_services
from tests.db.seed import Seeder
from tests.engine.graph_output_cases import child
from tests.engine.start_cases import prepared_once, start_rows
from tests.plugins.authentication.support import AuthorityClock, seed_case

__all__ = ["api_capture", "evaluator", "preparer"]

VERSIONS = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/versions"
RUNS = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/runs"
SERVICE_CREDENTIAL = "private-trigger-service-credential"


def error_of(response: httpx.Response) -> ApiErrorEnvelope:
    return ApiErrorEnvelope.model_validate(response.json())


def keyed(headers: dict[str, str], key: str) -> dict[str, str]:
    return headers | {"Idempotency-Key": key}


async def user(database: Database) -> tuple[dict[str, str], str]:
    case = await seed_case(database, NAMESPACE)
    return (
        {"Authorization": str(case.request.authorization)},
        case.principal.namespaces[0].namespace_id,
    )


async def service_headers(database: Database, namespace_id: str) -> dict[str, str]:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        principal = Principal.model_validate(
            {
                "token_id": seed.new_id(),
                "kind": "service",
                "owner": "private-trigger-service-owner",
                "namespaces": [{"namespace_id": namespace_id, "transcripts": False}],
                "approve": False,
                "platform": False,
                "runtime": "local",
            }
        )
        await connection.execute(
            insert(api_token).values(
                id=principal.token_id,
                digest=sha256(SERVICE_CREDENTIAL.encode()).hexdigest(),
                expires_at=None,
                **{
                    key: value
                    for key, value in principal.model_dump(mode="json").items()
                    if key != "token_id"
                },
                **seed.stamps(),
            )
        )
    return {"Authorization": "Bearer " + SERVICE_CREDENTIAL}


async def deployed(
    database: Database,
    evaluator: Evaluator,
    namespace_id: str,
    ir: JsonObject | None = None,
    policy: NamespaceSecretPolicy = DEFAULT_POLICY,
) -> str:
    published = await publish(
        database,
        await prepared_once(evaluator, ir, policy),
        namespace_id,
        IdentifierGenerator(AuthorityClock().now).new(),
    )
    return "gv_" + published.graph_version_id


def with_overlap(overlap: str) -> JsonObject:
    ir = skeleton_ir()
    child(ir, "graph")["overlap"] = overlap
    return ir


async def empty(database: Database) -> bool:
    return all(not table for table in (await start_rows(database)).values())


@pytest.mark.asyncio
async def test_trigger_route_accepts_and_replays(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        version = await deployed(database, evaluator, namespace_id)
        first = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "k1")
        )
        again = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "k1")
        )
        changed = await client.post(
            RUNS,
            json={"params": {}, "allow_unenforced_secrets": True},
            headers=keyed(headers, "k1"),
        )
        unkeyed = await client.post(RUNS, json={"params": {}}, headers=headers)

    accepted = AcceptedRun.model_validate(first.json())
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert accepted.graph_version_id == version
    assert accepted.run_id.startswith("run_")
    assert (changed.status_code, error_of(changed).error.code) == (409, "API004")
    assert unkeyed.status_code == 201
    assert AcceptedRun.model_validate(unkeyed.json()).run_id != accepted.run_id
    records = [
        record
        for record in api_capture.finish()
        if record["event"] == "api.request.completed"
        and record["route"] == "/api/v1/namespaces/{ns}/graphs/{graph}/runs"
    ]
    assert len(records) == 4
    rendered = json.dumps(records)
    assert all(
        private not in rendered
        for private in (NAMESPACE, accepted.run_id, "k1", headers["Authorization"])
    )


@pytest.mark.asyncio
async def test_trigger_route_skips_on_overlap(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        await deployed(database, evaluator, namespace_id, with_overlap("skip"))
        first = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "k1")
        )
        skipped = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "k2")
        )

    assert first.status_code == 201
    assert skipped.status_code == 200
    assert SkippedRun.model_validate(skipped.json()) == SkippedRun(
        outcome="skipped", reason="overlap", run_id=None
    )


@pytest.mark.asyncio
async def test_trigger_route_refuses_a_full_backlog(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy={"concurrency": {"max_pending_runs": 2}})
        )
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        await deployed(database, evaluator, namespace_id)
        for key in ("k1", "k2"):
            await client.post(RUNS, json={"params": {}}, headers=keyed(headers, key))
        async with database.transaction() as writer:
            await writer.execute(
                update(namespace)
                .where(namespace.c.id == namespace_id)
                .values(policy={"concurrency": {"max_pending_runs": 1}})
            )
        full = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "k3")
        )

    envelope = error_of(full)
    assert (full.status_code, envelope.error.code, envelope.error.retryable) == (
        409,
        "API002",
        True,
    )
    assert envelope.model_dump(mode="json")["error"]["details"] == {
        "reason": "run_backlog_full",
        "scope": {"kind": "namespace", "id": NAMESPACE},
        "limit_name": "max_pending_runs",
        "limit": 1,
        "used": 2,
        "next_action": "retry_start",
    }


@pytest.mark.asyncio
async def test_trigger_route_authorizes_before_body(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    service = await service_headers(database, namespace_id)
    await seed_case(database, "other")
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    malformed = b"{not json"

    async with client_for(app) as client:
        anonymous = await client.post(RUNS, content=malformed)
        worker = await client.post(RUNS, content=malformed, headers=service)
        worker_deploy = await client.post(VERSIONS, content=malformed, headers=service)
        outside = await client.post(
            "/api/v1/namespaces/other/graphs/skeleton/runs",
            content=malformed,
            headers=headers,
        )
        absent = await client.post(
            "/api/v1/namespaces/absent/graphs/skeleton/runs",
            content=malformed,
            headers=headers,
        )
        unknown = await client.post(RUNS, json={"params": {}}, headers=headers)
        wrong_method = await client.get(RUNS, headers=headers)

    assert (anonymous.status_code, error_of(anonymous).error.code) == (401, "API009")
    for refused in (worker, worker_deploy, outside, absent):
        assert (refused.status_code, error_of(refused).error.code) == (403, "API005")
    assert (unknown.status_code, error_of(unknown).error.code) == (404, "API001")
    assert (wrong_method.status_code, wrong_method.headers["allow"]) == (405, "POST")
    assert await empty(database)


@pytest.mark.asyncio
async def test_trigger_route_refuses_invalid_starts(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    other = application(
        database, api_capture, deployment_services(database, preparer, "claude")
    )
    content = {"Content-Type": "application/json"}
    requests: tuple[tuple[bytes, dict[str, str]], ...] = (
        (b"{not json", {}),
        (b'{"params": {}, "params": {}}', {}),
        (b'{"params": {"extra": 1}}', {}),
        (b'{"params": [], "graph_version": "gv_1"}', {}),
        (b'{"params": {}}', {"Idempotency-Key": "x" * 256}),
        (b'{"params": {}}', {"Idempotency-Key": "two words"}),
    )
    duplicated = [
        (b"authorization", headers["Authorization"].encode()),
        (b"content-type", b"application/json"),
        (b"idempotency-key", b"first"),
        (b"idempotency-key", b"second"),
    ]

    async with client_for(app) as client, client_for(other) as unconfigured:
        await deployed(database, evaluator, namespace_id)
        responses = [
            await client.post(RUNS, content=body, headers=headers | content | extra)
            for body, extra in requests
        ]
        installation = await unconfigured.post(
            RUNS, json={"params": {}}, headers=headers
        )
        twice = await client.post(
            RUNS, content=b'{"params": {}}', headers=httpx.Headers(duplicated)
        )

    for response in (*responses, twice):
        assert (response.status_code, error_of(response).error.code) == (422, "API003")
    findings = error_of(installation).model_dump(mode="json")["error"]["details"]
    assert error_of(installation).error.code == "API003"
    assert [finding["code"] for finding in findings["errors"]] == ["IR018"]
    assert await empty(database)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ("secrets", 422, "API003"),
        ("parameters", 500, "API010"),
        ("inactive", 500, "API010"),
    ],
)
async def test_trigger_route_refuses_unsupported_versions(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    change: str,
    status: int,
    code: str,
) -> None:
    headers, namespace_id = await user(database)
    await save_secret_policy(database, namespace_id, ALLOW_AGENT_SECRETS)
    ir = with_agent_secret() if change == "secrets" else skeleton_ir()
    if change == "parameters":
        child(ir, "graph")["params"] = {"type": "object"}
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        version = await deployed(
            database, evaluator, namespace_id, ir, ALLOW_AGENT_SECRETS
        )
        if change == "inactive":
            async with database.transaction() as writer:
                await writer.execute(
                    update(graph_version)
                    .where(graph_version.c.id == version.removeprefix("gv_"))
                    .values(status="retired")
                )
        response = await client.post(
            RUNS,
            json={"params": {}, "allow_unenforced_secrets": True},
            headers=headers,
        )

    assert (response.status_code, error_of(response).error.code) == (status, code)
    assert await empty(database)


@pytest.mark.asyncio
async def test_trigger_route_refuses_while_preparation_is_busy(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        await deployed(database, evaluator, namespace_id)
        first = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "k1")
        )
        async with preparer.hold():
            busy = await client.post(
                RUNS, json={"params": {}}, headers=keyed(headers, "k2")
            )
            replayed = await client.post(
                RUNS, json={"params": {}}, headers=keyed(headers, "k1")
            )
        admitted = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "k2")
        )

    assert (busy.status_code, error_of(busy).error.code) == (503, "API011")
    assert error_of(busy).error.retryable is True
    assert (replayed.status_code, replayed.json()) == (200, first.json())
    assert admitted.status_code == 201
