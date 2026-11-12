import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.types import Message

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import (
    api_token,
    event,
    worker,
    worker_execution_domain,
    worker_incarnation,
)
from kora.db.tokens import REVOKED
from kora.engine.worker_registration import Registration, register_worker
from kora.models.workers import WorkerRegistration
from tests.api.deployment_cases import HARNESS, preparer
from tests.api.support import HttpCall, api_capture, client_for, deployment_services
from tests.api.worker_support import (
    CAPABILITIES,
    bearer,
    registration,
    worker_application,
)
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.plugins.authentication.support import AuthorityClock, seed_case

__all__ = ["api_capture", "preparer"]


def changed(body: WorkerRegistration, **values: object) -> WorkerRegistration:
    return WorkerRegistration.model_validate({**body.model_dump(mode="json"), **values})


class DisconnectedWorkerBody(HttpCall):
    async def receive(self) -> Message:
        self.receives += 1
        if self.receives == 1:
            return {"type": "http.request", "body": b"{", "more_body": True}
        return {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_registration_disconnected_body_is_a_content_free_refusal(
    database: Database, api_capture: Capture
) -> None:
    service = await seed_case(database, "service", "service")
    app = worker_application(database, api_capture, AuthorityClock())
    call = DisconnectedWorkerBody(
        path="/api/v1/workers",
        method="POST",
        headers=[(b"authorization", (service.request.authorization or "").encode())],
    )

    await call.run(app)
    async with database.read_transaction() as connection:
        workers = (
            await connection.execute(select(func.count()).select_from(worker))
        ).scalar_one()
        events = (
            await connection.execute(select(func.count()).select_from(event))
        ).scalar_one()
    records = api_capture.finish()

    assert call.status == 422
    assert call.receives == 2
    assert b"API003" in call.body
    assert workers == events == 0
    assert "api.request.failed" not in {record["event"] for record in records}


@pytest.mark.asyncio
async def test_worker_registration_replays_exact_incarnation(
    database: Database,
    api_capture: Capture,
) -> None:
    case = await seed_case(database, "worker", "service")
    clock = AuthorityClock()
    app = worker_application(database, api_capture, clock)
    body = registration(case)
    ledger = ConnectionLedger(database)

    async with KoraClient("http://127.0.0.1", bearer(case), asgi_app=app) as client:
        first = await client.register_worker(body)
        clock.moment += timedelta(seconds=100)
        replay = await client.register_worker(body)
    async with database.read_transaction() as connection:
        current = (await connection.execute(select(worker))).mappings().one()
        history = (
            (await connection.execute(select(worker_incarnation))).mappings().one()
        )
        audit = (await connection.execute(select(event))).mappings().one()

    assert first == replay
    assert first.registered_at == START
    assert current["last_seen_at"] == current["updated_at"] == START
    assert history["process"] == body.process.model_dump(mode="json")
    assert ledger.outstanding == 0
    assert audit["namespace_id"] is None
    assert audit["kind"] == "worker.registered"
    assert audit["actor"] == {
        "kind": "service",
        "owner": case.principal.owner,
        "token_id": case.principal.token_id,
    }
    assert audit["payload"] == {"runtime": body.runtime, "version": body.version}
    records = api_capture.finish()
    outcomes = [
        record for record in records if record["event"] == "engine.worker.registered"
    ]
    assert [record["replayed"] for record in outcomes] == [False, True]
    assert bearer(case) not in str(records)
    assert body.process.host_id is not None
    assert body.process.host_id not in str(outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["version", "capacity", "execution_domain_id", "process"]
)
async def test_worker_registration_preserves_incarnation_process(
    database: Database,
    api_capture: Capture,
    field: str,
) -> None:
    case = await seed_case(database, "worker", "service")
    body = registration(case)
    values: dict[str, object] = {
        "version": "0.2.0",
        "capacity": 2,
        "execution_domain_id": "other-domain",
        "process": {**body.process.model_dump(mode="json"), "leads_group": False},
    }
    app = worker_application(database, api_capture, AuthorityClock())

    async with KoraClient("http://127.0.0.1", bearer(case), asgi_app=app) as client:
        first = await client.register_worker(body)
        with pytest.raises(ApiError) as refusal:
            await client.register_worker(changed(body, **{field: values[field]}))
        replay = await client.register_worker(body)
    async with database.read_transaction() as connection:
        domains = (
            await connection.execute(
                select(func.count()).select_from(worker_execution_domain)
            )
        ).scalar_one()

    assert refusal.value.code == "API002"
    assert replay == first
    assert domains == 1


@pytest.mark.asyncio
async def test_registration_restart_preserves_old_replay_and_process_uniqueness(
    database: Database,
    api_capture: Capture,
) -> None:
    case = await seed_case(database, "worker", "service")
    body = registration(case)
    clock = AuthorityClock()
    app = worker_application(database, api_capture, clock)
    second = changed(
        body,
        incarnation_id="second",
        process={
            **body.process.model_dump(mode="json"),
            "start_time": 501,
        },
    )

    async with KoraClient("http://127.0.0.1", bearer(case), asgi_app=app) as client:
        first = await client.register_worker(body)
        with pytest.raises(ApiError) as refusal:
            await client.register_worker(changed(body, incarnation_id="duplicate"))
        clock.moment += timedelta(seconds=10)
        restarted = await client.register_worker(second)
        replay = await client.register_worker(body)
    async with database.read_transaction() as connection:
        current = (await connection.execute(select(worker))).mappings().one()
        count = (
            await connection.execute(
                select(func.count()).select_from(worker_incarnation)
            )
        ).scalar_one()

    assert refusal.value.code == "API002"
    assert replay == first
    assert restarted.registered_at == clock.moment
    assert current["incarnation_id"] == "second"
    assert count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["owner", "runtime", "token", "domain"])
async def test_worker_registration_refuses_foreign_token_runtime_or_domain(
    database: Database,
    api_capture: Capture,
    mismatch: str,
) -> None:
    first = await seed_case(database, "first", "service")
    second = await seed_case(database, "second", "service")
    body = registration(first)
    app = worker_application(database, api_capture, AuthorityClock())
    async with KoraClient("http://127.0.0.1", bearer(first), asgi_app=app) as client:
        await client.register_worker(body)
    other = changed(
        body,
        incarnation_id="other",
        process={
            **body.process.model_dump(mode="json"),
            "pid": 1235,
        },
    )
    if mismatch == "runtime":
        other = changed(other, name=second.principal.owner, runtime="other")
    elif mismatch == "domain":
        other = changed(other, name=second.principal.owner)
    elif mismatch == "token":
        async with database.transaction() as connection:
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == second.principal.token_id)
                .values(owner=first.principal.owner)
            )

    async with KoraClient("http://127.0.0.1", bearer(second), asgi_app=app) as client:
        with pytest.raises(ApiError) as refusal:
            await client.register_worker(other)
    async with database.read_transaction() as connection:
        current = (await connection.execute(select(worker))).mappings().one()

    assert refusal.value.code == ("API002" if mismatch == "domain" else "API005")
    assert current["token_id"] == first.principal.token_id
    assert current["incarnation_id"] == body.incarnation_id


@pytest.mark.asyncio
async def test_worker_route_principal_separation(
    database: Database, api_capture: Capture, preparer: DeploymentPreparer
) -> None:
    personal = await seed_case(database, "personal")
    service = await seed_case(database, "service", "service")
    app = worker_application(
        database,
        api_capture,
        AuthorityClock(),
        deployment=deployment_services(database, preparer, HARNESS),
    )
    call = HttpCall(
        path="/api/v1/workers",
        method="POST",
        headers=[
            (b"authorization", (personal.request.authorization or "").encode()),
        ],
    )
    deploy_call = HttpCall(
        path="/api/v1/namespaces/example/graphs/example/versions",
        method="POST",
        headers=[
            (b"authorization", (service.request.authorization or "").encode()),
        ],
    )

    await call.run(app)
    await deploy_call.run(app)
    async with KoraClient("http://127.0.0.1", bearer(service), asgi_app=app) as client:
        with pytest.raises(ApiError) as refusal:
            await client.openapi()
        registered = await client.register_worker(registration(service))
    async with KoraClient("http://127.0.0.1", bearer(personal), asgi_app=app) as client:
        schema = await client.openapi()

    assert call.status == 403
    assert call.receives == 0
    assert deploy_call.status == 403
    assert deploy_call.receives == 0
    assert refusal.value.code == "API005"
    assert registered.worker_id == service.principal.owner
    paths = schema["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == {
        "/healthz",
        "/api/v1/namespaces/{ns}/graphs/{graph}/versions",
        "/api/v1/workers",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True], ids=["asgi", "http"])
async def test_worker_client_http_and_asgi_have_identical_authorization(
    database: Database,
    api_capture: Capture,
    network: bool,
) -> None:
    service = await seed_case(database, "service", "service")
    app = worker_application(database, api_capture, AuthorityClock())
    body = registration(service)
    ledger = ConnectionLedger(database)

    async with running_app(app) as origin:
        async with KoraClient(
            origin if network else "http://127.0.0.1",
            bearer(service),
            asgi_app=None if network else app,
        ) as client:
            result = await client.register_worker(body)
            async with database.transaction() as connection:
                await connection.execute(
                    update(api_token)
                    .where(api_token.c.id == service.principal.token_id)
                    .values(expires_at=REVOKED)
                )
            with pytest.raises(ApiError) as refusal:
                await client.register_worker(body)

    assert result.worker_id == body.name
    assert refusal.value.code == "API009"
    assert ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("portable", [False, True])
async def test_worker_domain_concurrent_registration_has_one_nonportable_owner(
    database: Database,
    api_capture: Capture,
    portable: bool,
) -> None:
    cases = [await seed_case(database, str(index), "service") for index in range(2)]
    app = worker_application(
        database,
        api_capture,
        AuthorityClock(),
        replace(CAPABILITIES, portable_handles=portable),
    )
    barrier = asyncio.Barrier(2)

    async def register(index: int) -> str:
        case = cases[index]
        body = registration(case)
        body = changed(
            body, process={**body.process.model_dump(mode="json"), "pid": 1234 + index}
        )
        async with KoraClient("http://127.0.0.1", bearer(case), asgi_app=app) as client:
            await barrier.wait()
            try:
                await client.register_worker(body)
            except ApiError as error:
                return error.code
            return "accepted"

    async with asyncio.timeout(10):
        results = await asyncio.gather(register(0), register(1))
    async with database.read_transaction() as connection:
        rows = (
            (await connection.execute(select(worker_execution_domain))).mappings().all()
        )
        count = (
            await connection.execute(select(func.count()).select_from(worker))
        ).scalar_one()

    assert sorted(results) == (
        ["accepted", "accepted"] if portable else ["API002", "accepted"]
    )
    assert len(rows) == 1
    assert count == (2 if portable else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"capacity": True},
        {"capacity": 0},
        {"capacity": 2**31},
        {"unknown": "secret"},
        {"process": {"pid": 1}},
    ],
)
async def test_worker_registration_rejects_invalid_body_without_effects(
    database: Database,
    api_capture: Capture,
    invalid: dict[str, object],
) -> None:
    case = await seed_case(database, "worker", "service")
    body = registration(case).model_dump(mode="json") | invalid
    app = worker_application(database, api_capture, AuthorityClock())

    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/workers",
            json=body,
            headers={"Authorization": case.request.authorization or ""},
        )
    async with database.read_transaction() as connection:
        count = (
            await connection.execute(select(func.count()).select_from(worker))
        ).scalar_one()

    assert response.status_code == 422
    assert count == 0
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_worker_registration_audit_supports_empty_grants_and_namespace_filtering(
    database: Database,
    api_capture: Capture,
) -> None:
    case = await seed_case(database, "empty", "service")
    namespace = case.principal.namespaces[0].namespace_id
    async with database.transaction() as connection:
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == case.principal.token_id)
            .values(namespaces=[])
        )
    app = worker_application(database, api_capture, AuthorityClock())

    async with KoraClient("http://127.0.0.1", bearer(case), asgi_app=app) as client:
        await client.register_worker(registration(case))
        await client.register_worker(registration(case))
    async with database.read_transaction() as connection:
        count = (
            await connection.execute(select(func.count()).select_from(event))
        ).scalar_one()
        scoped = (
            await connection.execute(
                select(event).where(event.c.namespace_id == namespace)
            )
        ).all()

    assert count == 1
    assert scoped == []


@pytest.mark.asyncio
async def test_worker_registration_audit_failure_rolls_back_every_effect(
    database: Database,
    api_capture: Capture,
) -> None:
    case = await seed_case(database, "worker", "service")
    body = registration(case)
    app = worker_application(database, api_capture, AuthorityClock())
    async with KoraClient("http://127.0.0.1", bearer(case), asgi_app=app) as client:
        await client.register_worker(body)
    async with database.read_transaction() as connection:
        identity = (await connection.execute(select(event.c.id))).scalar_one()
    assert isinstance(identity, str)
    next_body = changed(
        body,
        incarnation_id="second",
        execution_domain_id="new-domain",
        process={**body.process.model_dump(mode="json"), "pid": 1235},
    )
    next_registration = Registration(
        next_body.name,
        next_body.incarnation_id,
        next_body.runtime,
        next_body.execution_domain_id,
        next_body.version,
        next_body.capacity,
        next_body.process,
    )

    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await register_worker(
                connection,
                next_registration,
                case.principal,
                portable=False,
                now=START,
                event_id=identity,
            )
    async with database.read_transaction() as connection:
        current = (
            await connection.execute(select(worker.c.incarnation_id))
        ).scalar_one()
        counts = [
            (
                await connection.execute(select(func.count()).select_from(table))
            ).scalar_one()
            for table in (worker_incarnation, worker_execution_domain, event)
        ]

    assert current == body.incarnation_id
    assert counts == [1, 1, 1]


@pytest.mark.asyncio
async def test_worker_registration_openapi_and_method_parity_exclusion(
    database: Database,
    api_capture: Capture,
) -> None:
    personal = await seed_case(database, "schema")
    service = await seed_case(database, "worker", "service")
    app = worker_application(database, api_capture, AuthorityClock())

    async with client_for(app) as client:
        schema = await client.get(
            "/api/v1/openapi.json",
            headers={"Authorization": personal.request.authorization or ""},
        )
        refused = await client.get(
            "/api/v1/workers",
            headers={"Authorization": service.request.authorization or ""},
        )

    assert set(schema.json()["paths"]) == {"/healthz", "/api/v1/workers"}
    operations = schema.json()["paths"]["/api/v1/workers"]
    assert set(operations) == {"post"}
    assert operations["post"]["operationId"] == "register_worker"
    assert operations["post"]["requestBody"]["required"] is True
    assert (
        schema.json()["components"]["schemas"]["WorkerRegistration"][
            "additionalProperties"
        ]
        is False
    )
    assert refused.status_code == 405
    assert refused.headers["Allow"] == "POST"
