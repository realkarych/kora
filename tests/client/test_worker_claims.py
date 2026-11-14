import math
from collections.abc import AsyncIterator
from typing import cast

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from kora.client import ClientError, KoraClient
from kora.client.errors import Failure
from kora.ir.graph_json import ObjectReader, TextBudget, render_object
from kora.ir.json import JsonObject, JsonValue
from kora.models.worker_claims import IMMUTABLE_FIELDS, WorkerClaimRequest
from kora.protocols.worker_claim_policy import prepare_claim_policy, prepare_node_policy
from kora.protocols.worker_claim_system import ClaimSystem
from kora.protocols.worker_claim_wire import (
    MAX_CLAIM_BYTES,
    MAX_IMMUTABLE_BYTES,
    ClaimPartition,
    prepare_claim_part,
)
from tests.client.support import running_app
from tests.engine.test_claim_material import PRICE
from tests.protocols.test_worker_claim_system import system_values

REQUEST = WorkerClaimRequest(
    runtime="local",
    incarnation_id="incarnation",
    execution_domain_id="domain",
    max=1,
    claim_id="claim",
)


def claim_document() -> JsonObject:
    system = system_values()
    system.update(
        handle=None, execution_request=None, permitted_actions=["observe", "retire"]
    )
    result = cast(
        JsonObject, ClaimSystem.model_validate(system).model_dump(mode="json")
    )
    result.update(
        params={"int": 1, "float": 1.0, "zero": -0.0},
        resolved_config={},
        execution_context={
            "version": 1,
            "kind": "command",
            "graph_version_id": "version",
            "execution_contract_digest": "a" * 64,
            "execution_semantics": "kora.execution.v1",
            "runtime": "local",
            "vcs": "git",
            "protected_paths": [],
            "retry_policy": {},
        },
        input_bindings={},
        output_contract=[],
        limits={"stall_after_us": "1"},
        secret_aliases={},
        resources=[],
        price_table_id=PRICE.id,
        price_table=cast(JsonObject, PRICE.model_dump(mode="json")),
    )
    result.update(
        ObjectReader(
            prepare_claim_policy({}, 1, prepare_node_policy({})).payload.decode()
        ).object()
    )
    return {"claim": result}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize("name", ["worker", ".", "../path/%?\\"])
async def test_claim_client_preserves_graph_values_and_worker_name(
    transport: str, name: str
) -> None:
    document = claim_document()
    raw = cast(JsonObject, document["claim"])
    leaf: JsonValue = {
        "string": "\ud800",
        "huge": 10**5000,
        "float": 1.0,
        "int": 1,
        "zero": -0.0,
    }
    nested: JsonValue = leaf
    for _ in range(300):
        nested = [nested]
    raw["resolved_config"] = {"deep": nested}
    body = render_object(
        document, TextBudget(MAX_CLAIM_BYTES, ascii_only=False)
    ).encode()
    seen: list[tuple[str, bytes]] = []

    async def endpoint(request: Request) -> Response:
        seen.append((str(request.path_params["worker_id"]), await request.body()))
        return Response(body, media_type="application/json")

    app = Starlette(
        routes=[
            Route("/api/v1/workers/{worker_id:path}/claims", endpoint, methods=["POST"])
        ]
    )

    async with running_app(app) as origin:
        async with KoraClient(
            "http://127.0.0.1" if transport == "asgi" else origin,
            "service",
            asgi_app=app if transport == "asgi" else None,
        ) as client:
            result = await client.claim_worker(name, REQUEST)

    assert result.claim is not None
    value = result.claim.resolved_config["deep"]
    for _ in range(300):
        assert isinstance(value, list)
        value = value[0]
    assert value == leaf
    assert isinstance(value, dict)
    assert type(value["float"]) is float
    assert type(value["int"]) is int
    assert math.copysign(1.0, cast(float, value["zero"])) == -1
    assert result.claim.params == {"int": 1, "float": 1.0, "zero": -0.0}
    assert seen == [(name, REQUEST.model_dump_json().encode())]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["integer", "depth", "policy", "missing", "syntax"])
async def test_claim_client_refuses_invalid_partitions_and_json(fault: str) -> None:
    document = claim_document()
    raw = cast(JsonObject, document["claim"])
    if fault == "integer":
        raw["params"] = {"large": 2**63}
    elif fault == "depth":
        nested: JsonValue = []
        for _ in range(33):
            nested = [nested]
        raw["params"] = {"deep": nested}
    elif fault == "policy":
        cast(JsonObject, raw["execution_policy"])["extra"] = True
    elif fault == "missing":
        del raw["acknowledged_segment_offset"]
    body = render_object(
        document, TextBudget(MAX_CLAIM_BYTES, ascii_only=False)
    ).encode()
    if fault == "syntax":
        body = body.replace(b'"lease_epoch":9223372036854775807', b'"lease_epoch":1 2')

    async def endpoint(request: Request) -> Response:
        return Response(body, media_type="application/json")

    app = Starlette(
        routes=[Route("/api/v1/workers/worker/claims", endpoint, methods=["POST"])]
    )

    async with KoraClient("http://127.0.0.1", "service", asgi_app=app) as client:
        with pytest.raises(ClientError) as refusal:
            await client.claim_worker("worker", REQUEST)

    assert refusal.value.reason is (
        Failure.JSON if fault == "syntax" else Failure.SCHEMA
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize("excess", [0, 1])
async def test_claim_client_enforces_exact_wire_limit_on_both_transports(
    transport: str, excess: int
) -> None:
    document = claim_document()
    raw = cast(JsonObject, document["claim"])
    config: JsonObject = {"prompt": ""}
    raw["resolved_config"] = config
    partition = prepare_claim_part(
        ClaimPartition.IMMUTABLE, {name: raw[name] for name in IMMUTABLE_FIELDS}
    )
    config["prompt"] = "a" * (MAX_IMMUTABLE_BYTES - len(partition.payload))
    body = render_object(
        document, TextBudget(MAX_CLAIM_BYTES, ascii_only=False)
    ).encode()
    body += b" " * (MAX_CLAIM_BYTES + excess - len(body))
    calls = 0

    async def chunks() -> AsyncIterator[bytes]:
        for position in range(0, len(body), 65_536):
            yield body[position : position + 65_536]

    async def endpoint(request: Request) -> Response:
        nonlocal calls
        calls += 1
        return (
            StreamingResponse(chunks(), media_type="application/json")
            if calls == 1
            else Response(b'{"claim":null}', media_type="application/json")
        )

    app = Starlette(
        routes=[Route("/api/v1/workers/worker/claims", endpoint, methods=["POST"])]
    )

    async with running_app(app) as origin:
        async with KoraClient(
            "http://127.0.0.1" if transport == "asgi" else origin,
            "service",
            asgi_app=app if transport == "asgi" else None,
        ) as client:
            if excess:
                with pytest.raises(ClientError) as refusal:
                    await client.claim_worker("worker", REQUEST)
                assert refusal.value.reason is Failure.RESPONSE_LIMIT
            else:
                accepted = await client.claim_worker("worker", REQUEST)
                assert accepted.claim is not None
                assert accepted.claim.resolved_config == config
            subsequent = await client.claim_worker("worker", REQUEST)

    assert len(body) == MAX_CLAIM_BYTES + excess
    assert subsequent.claim is None
    assert calls == 2
