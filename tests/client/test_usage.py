import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from kora.client import KoraClient
from kora.models.worker_usage import UsageReport
from tests.client.support import running_app
from tests.engine.test_usage_acceptance import coverage, usage


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True])
async def test_usage_client_accepts_additive_response_fields_without_filtering_source(
    network: bool,
) -> None:
    body = UsageReport(
        lease_epoch=1,
        dispatcher_id="grant",
        rows=(usage(source={"future": "opaque", "nested": {"future": 1.0}}),),
        coverage=coverage(),
    )

    async def endpoint(request: Request) -> JSONResponse:
        assert request.headers["authorization"] == "Bearer service"
        assert await request.json() == body.model_dump(mode="json")
        return JSONResponse(
            {
                "rows": [
                    body.rows[0].model_dump(mode="json")
                    | {"usd_estimate": None, "future": {"opaque": True}}
                ],
                "coverage": body.coverage.model_dump(mode="json") | {"future": True},
                "future": True,
            }
        )

    app = Starlette(
        routes=[Route("/api/v1/attempts/attempt/usage", endpoint, methods=["POST"])]
    )

    async with running_app(app) as origin:
        async with KoraClient(
            origin if network else "http://127.0.0.1",
            "service",
            asgi_app=None if network else app,
        ) as client:
            result = await client.report_usage("attempt", body)

    assert result.rows[0].source == body.rows[0].source
    assert result.rows[0].usd_estimate is None
    assert result.coverage == body.coverage
