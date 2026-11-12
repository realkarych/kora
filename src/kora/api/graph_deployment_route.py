import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kora.api.errors import (
    DependencyRetryContext,
    RequestRejection,
    definitive_error,
    dependency_unavailable,
    deployment_validation_error,
    internal_error,
    request_rejection,
)
from kora.api.exchange import current_exchange
from kora.api.graph_deployment import (
    DeploymentFindings,
    DeploymentRefusal,
    prepare_graph_deployment,
)
from kora.api.graph_deployment_limits import MAX_REQUEST_BYTES
from kora.api.graph_deployment_writer import (
    GraphDeploymentTarget,
    publish_graph_deployment,
)
from kora.api.responses import error_response
from kora.cel_eval.errors import EvaluatorUnavailable
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.namespace_reads import granted_namespace_id
from kora.engine.graph_command_manifests import GraphDeploymentSelectors
from kora.ir.deployment_checks import DeploymentSelection
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope
from kora.models.graphs import GRAPH_VERSION_PREFIX, DeployRequest, DeployResponse
from kora.protocols.authentication import Principal
from kora.protocols.time import Clock


@dataclass(frozen=True, slots=True)
class GraphDeploymentServices:
    database: Database
    evaluator: Evaluator
    selection: DeploymentSelection
    identifiers: IdentifierGenerator


class BodyRejected(ValueError):
    def __init__(self, reason: RequestRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise BodyRejected(RequestRejection.MALFORMED_JSON)
    return document


def _finite_only(token: str) -> object:
    raise BodyRejected(RequestRejection.MALFORMED_JSON)


async def _read_body(request: Request) -> DeployRequest:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            raise BodyRejected(RequestRejection.BYTE_LIMIT)
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        raise BodyRejected(RequestRejection.INVALID_ENCODING) from None
    try:
        parsed: object = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_finite_only
        )
    except (ValueError, RecursionError):
        raise BodyRejected(RequestRejection.MALFORMED_JSON) from None
    try:
        return DeployRequest.model_validate(parsed)
    except ValidationError:
        raise BodyRejected(RequestRejection.FRAMEWORK_SHAPE) from None


async def _authorized_namespace(
    database: Database, principal: Principal, name: str
) -> str | None:
    granted = tuple(grant.namespace_id for grant in principal.namespaces)

    async def unit(connection: AsyncConnection) -> str | None:
        return await granted_namespace_id(connection, name, granted)

    return await database.read(unit)


DeploymentHandler = Callable[[Request, str, str], Awaitable[Response]]
REFUSALS: dict[DeploymentRefusal, RequestRejection] = {
    DeploymentRefusal.DOCUMENT: RequestRejection.DOCUMENT,
    DeploymentRefusal.SELECTORS: RequestRejection.SELECTORS,
}


async def _deploy(
    services: GraphDeploymentServices,
    clock: Clock,
    request: Request,
    principal: Principal,
    names: tuple[str, str],
) -> ApiErrorEnvelope | Response:
    request_id = current_exchange().request_id
    namespace_name, graph = names
    namespace_id = await _authorized_namespace(
        services.database, principal, namespace_name
    )
    if namespace_id is None:
        return definitive_error(ApiErrorCode.FORBIDDEN, request_id=request_id)
    try:
        body = await _read_body(request)
    except BodyRejected as rejected:
        return request_rejection(rejected.reason, request_id=request_id)
    selectors = GraphDeploymentSelectors(
        authorized_namespace_name=namespace_name,
        route_graph=graph,
        request_graph=body.graph,
    )
    try:
        outcome = await prepare_graph_deployment(
            dict(body.ir), selectors, services.selection, services.evaluator
        )
    except EvaluatorUnavailable:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
    if isinstance(outcome, DeploymentFindings):
        return deployment_validation_error(outcome.diagnostics, request_id=request_id)
    if outcome is DeploymentRefusal.INCOMPLETE:
        return internal_error(request_id=request_id)
    if isinstance(outcome, DeploymentRefusal):
        return request_rejection(REFUSALS[outcome], request_id=request_id)
    published = await publish_graph_deployment(
        services.database,
        outcome,
        GraphDeploymentTarget(namespace_id, graph, principal),
        identifiers=services.identifiers,
        at=clock.now(),
    )
    digests = published.digests
    response = DeployResponse(
        graph_version_id=GRAPH_VERSION_PREFIX + published.graph_version_id,
        number=published.number,
        ir_digest=digests.ir_digest,
        execution_contract_digest=digests.execution_contract_digest,
        deployment_digest=digests.deployment_digest,
        reused=published.reused,
        warnings=outcome.warnings.warnings,
    )
    return JSONResponse(
        response.model_dump(mode="json"), status_code=200 if published.reused else 201
    )


def deployment_handler(
    services: GraphDeploymentServices, clock: Clock
) -> DeploymentHandler:
    async def deploy(request: Request, ns: str, graph: str) -> Response:
        principal = current_exchange().principal
        if principal is None:
            raise RuntimeError("A deployment requires an authenticated principal.")
        result = await _deploy(services, clock, request, principal, (ns, graph))
        if isinstance(result, ApiErrorEnvelope):
            return error_response(result, method=request.method)
        return result

    return deploy
