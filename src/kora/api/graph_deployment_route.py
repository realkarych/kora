import json
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import ClientDisconnect, Request
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
from kora.api.graph_deployment import DeploymentFindings, DeploymentRefusal
from kora.api.graph_deployment_limits import MAX_REQUEST_BYTES
from kora.api.graph_deployment_writer import (
    GraphDeploymentTarget,
    NamespacePolicyChanged,
    publish_graph_deployment,
)
from kora.api.graph_services import GraphServices
from kora.api.responses import error_response
from kora.cel_eval.errors import EvaluatorUnavailable
from kora.db.connection import Database
from kora.db.namespace_reads import (
    granted_namespace_id,
    read_namespace_secret_policy,
)
from kora.engine.graph_command_manifests import GraphDeploymentSelectors
from kora.ir.agent_secrets import NamespaceSecretPolicy
from kora.ir.vcs_ref_json import finite_float, refuse_constant, unique_object
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope
from kora.models.graphs import GRAPH_VERSION_PREFIX, DeployRequest, DeployResponse
from kora.protocols.authentication import Principal
from kora.protocols.time import Clock

Body = TypeVar("Body", bound=BaseModel)


class BodyRejected(ValueError):
    def __init__(self, reason: RequestRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


async def read_json_body(request: Request, model: type[Body], max_bytes: int) -> Body:
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise BodyRejected(RequestRejection.BYTE_LIMIT)
    except ClientDisconnect:
        raise BodyRejected(RequestRejection.INCOMPLETE_BODY) from None
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        raise BodyRejected(RequestRejection.INVALID_ENCODING) from None
    try:
        parsed: object = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=refuse_constant,
            parse_float=finite_float,
        )
    except (ValueError, RecursionError):
        raise BodyRejected(RequestRejection.MALFORMED_JSON) from None
    try:
        return model.model_validate(parsed)
    except ValidationError:
        raise BodyRejected(RequestRejection.FRAMEWORK_SHAPE) from None


async def authorized_namespace(
    database: Database, principal: Principal, name: str
) -> str | None:
    granted = tuple(grant.namespace_id for grant in principal.namespaces)

    async def unit(connection: AsyncConnection) -> str | None:
        return await granted_namespace_id(connection, name, granted)

    return await database.read(unit)


async def namespace_secret_policy(
    database: Database, namespace_id: str
) -> NamespaceSecretPolicy:
    async def unit(connection: AsyncConnection) -> NamespaceSecretPolicy:
        return await read_namespace_secret_policy(connection, namespace_id)

    return await database.read(unit)


DeploymentHandler = Callable[[Request, str, str], Awaitable[Response]]
REFUSALS: dict[DeploymentRefusal, RequestRejection] = {
    DeploymentRefusal.DOCUMENT: RequestRejection.DOCUMENT,
    DeploymentRefusal.TOO_LARGE: RequestRejection.BYTE_LIMIT,
    DeploymentRefusal.SELECTORS: RequestRejection.SELECTORS,
}


async def _deploy(
    services: GraphServices,
    clock: Clock,
    request: Request,
    principal: Principal,
    names: tuple[str, str],
) -> ApiErrorEnvelope | Response:
    request_id = current_exchange().request_id
    namespace_name, graph = names
    namespace_id = await authorized_namespace(
        services.database, principal, namespace_name
    )
    if namespace_id is None:
        return definitive_error(ApiErrorCode.FORBIDDEN, request_id=request_id)
    if services.preparer.busy:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
    try:
        body = await read_json_body(request, DeployRequest, MAX_REQUEST_BYTES)
    except BodyRejected as rejected:
        return request_rejection(rejected.reason, request_id=request_id)
    selectors = GraphDeploymentSelectors(
        authorized_namespace_name=namespace_name,
        route_graph=graph,
        request_graph=body.graph,
    )
    policy = await namespace_secret_policy(services.database, namespace_id)
    try:
        outcome = await services.preparer.prepare(
            dict(body.ir), selectors, services.selection, policy
        )
    except EvaluatorUnavailable:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
    if isinstance(outcome, DeploymentFindings):
        return deployment_validation_error(outcome.diagnostics, request_id=request_id)
    if outcome is DeploymentRefusal.INCOMPLETE:
        return internal_error(request_id=request_id)
    if outcome is DeploymentRefusal.BUSY:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
    if isinstance(outcome, DeploymentRefusal):
        return request_rejection(REFUSALS[outcome], request_id=request_id)
    try:
        published = await publish_graph_deployment(
            services.database,
            outcome,
            GraphDeploymentTarget(namespace_id, principal),
            identifiers=services.identifiers,
            at=clock.now(),
        )
    except NamespacePolicyChanged:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
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


def deployment_handler(services: GraphServices, clock: Clock) -> DeploymentHandler:
    async def deploy(request: Request, ns: str, graph: str) -> Response:
        principal = current_exchange().principal
        if principal is None:
            raise RuntimeError("A deployment requires an authenticated principal.")
        result = await _deploy(services, clock, request, principal, (ns, graph))
        if isinstance(result, ApiErrorEnvelope):
            return error_response(result, method=request.method)
        return result

    return deploy
