import pytest
from sqlalchemy import update

from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import (
    api_token,
    attempt,
    graph_version,
    node_instance,
)
from tests.api.support import HttpCall, api_capture
from tests.api.usage_support import report, usage_application, usage_path
from tests.api.worker_support import bearer
from tests.engine.worker_usage_support import usage_case, usage_snapshot
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["missing", "user", "unknown"])
async def test_usage_api_authenticates_before_reading_body(
    database: Database, api_capture: Capture, credential: str
) -> None:
    case = await usage_case(database)
    personal = await seed_case(database, "personal")
    authorization = {
        "missing": "",
        "user": personal.request.authorization or "",
        "unknown": "Bearer unknown",
    }[credential]
    call = HttpCall(
        path=usage_path(case),
        method="POST",
        headers=[(b"authorization", authorization.encode())],
    )
    before = await usage_snapshot(database, case)

    await call.run(usage_application(database, api_capture, case))
    after = await usage_snapshot(database, case)

    assert call.receives == 0
    assert call.status == (403 if credential == "user" else 401)
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("same_namespace", [False, True])
@pytest.mark.parametrize("corrupt_binding", ["none", "binding", "coverage"])
async def test_usage_api_hides_foreign_namespace_and_refuses_other_worker(
    database: Database,
    api_capture: Capture,
    same_namespace: bool,
    corrupt_binding: str,
) -> None:
    case = await usage_case(database)
    other = await seed_case(database, "other", "service")
    if corrupt_binding == "binding":
        async with database.transaction() as connection:
            await connection.execute(update(node_instance).values(node_path="x" * 1025))
    elif corrupt_binding == "coverage":
        async with database.transaction() as connection:
            await connection.execute(
                update(attempt).values(usage_coverage={"private": "x" * 60000})
            )
    if same_namespace:
        async with database.transaction() as connection:
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == other.principal.token_id)
                .values(namespaces=case.principal.model_dump(mode="json")["namespaces"])
            )
    app = usage_application(database, api_capture, case)
    before = await usage_snapshot(database, case)

    async with KoraClient("http://127.0.0.1", bearer(other), asgi_app=app) as client:
        with pytest.raises(ApiError) as refusal:
            await client.report_usage(case.attempt_id, report(case))
        with pytest.raises(ApiError) as missing:
            await client.report_usage("absent", report(case))
    after = await usage_snapshot(database, case)

    assert refusal.value.code == ("API005" if same_namespace else "API001")
    assert missing.value.code == "API001"
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["contract", "coverage", "binding"])
async def test_usage_api_stale_grant_precedes_corrupt_contract(
    database: Database, api_capture: Capture, corrupt: str
) -> None:
    case = await usage_case(database)
    app = usage_application(database, api_capture, case)
    async with database.transaction() as connection:
        if corrupt == "contract":
            await connection.execute(
                update(graph_version).values(ir={"private": "bad"})
            )
        elif corrupt == "coverage":
            await connection.execute(
                update(attempt).values(usage_coverage={"private": "x" * 60000})
            )
        else:
            await connection.execute(update(node_instance).values(node_path="x" * 1025))
    before = await usage_snapshot(database, case)

    async with KoraClient(
        "http://127.0.0.1", case.bearer_token, asgi_app=app
    ) as client:
        with pytest.raises(ApiError) as stale:
            await client.report_usage(case.attempt_id, report(case, lease_epoch=2))
        with pytest.raises(ApiError) as corrupt_refusal:
            await client.report_usage(case.attempt_id, report(case))
    after = await usage_snapshot(database, case)
    records = api_capture.finish()

    assert stale.value.code == "API008"
    assert corrupt_refusal.value.code == "API010"
    assert before == after
    assert "private" not in str(records)
    assert sum(record["event"] == "api.request.failed" for record in records) == 1
