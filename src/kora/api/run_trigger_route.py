import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kora.api.errors import (
    DependencyRetryContext,
    RequestRejection,
    backlog_full_error,
    definitive_error,
    dependency_unavailable,
    deployment_validation_error,
    internal_error,
    request_rejection,
)
from kora.api.exchange import current_exchange
from kora.api.graph_deployment_limits import IDENTITY, MAX_REQUEST_BYTES
from kora.api.graph_deployment_route import (
    BodyRejected,
    authorized_namespace,
    read_json_body,
)
from kora.api.graph_services import GraphServices
from kora.api.responses import error_response
from kora.cel_eval.thread import Evaluator
from kora.db.graph_deployment_reads import graph_id_by_name
from kora.db.types import NAME_LENGTH
from kora.engine.start_acceptance import (
    AcceptanceOutcome,
    StartAcceptance,
    StartCandidate,
    StartPreparation,
    StartResult,
    accept_manual_start,
    acquire_manual_start,
)
from kora.engine.start_facts import (
    InstallationRefusal,
    StartFactsResult,
    StartRefusal,
    read_manual_start_facts,
)
from kora.engine.start_records import (
    AcceptedStart,
    PreparedManualStartRequest,
    StartOutcome,
    StartRecordRejected,
    prepare_manual_start_request,
)
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope
from kora.models.graphs import GRAPH_VERSION_PREFIX
from kora.models.runs import RUN_PREFIX, AcceptedRun, SkippedRun, TriggerRequest
from kora.protocols.authentication import Principal
from kora.protocols.time import Clock

TriggerHandler = Callable[[Request, str, str], Awaitable[Response]]
DEFINITIVE: dict[AcceptanceOutcome, ApiErrorCode] = {
    AcceptanceOutcome.MISMATCHED: ApiErrorCode.IDEMPOTENCY_MISMATCH,
    AcceptanceOutcome.REFUSED: ApiErrorCode.FORBIDDEN,
    AcceptanceOutcome.MISSING: ApiErrorCode.NOT_FOUND,
}
UNAVAILABLE_INTERPRETATIONS: frozenset[AcceptanceOutcome] = frozenset(
    {AcceptanceOutcome.UNAVAILABLE, AcceptanceOutcome.RETENTION_UNAVAILABLE}
)
REJECTIONS: dict[StartRefusal, RequestRejection] = {
    StartRefusal.PARAMETERS: RequestRejection.PARAMETERS,
    StartRefusal.SECRETS: RequestRejection.SECRETS,
}


def idempotency_key(request: Request) -> str | None | RequestRejection:
    values = request.headers.getlist("idempotency-key")
    if not values:
        return None
    [value, *duplicates] = values
    if (
        duplicates
        or not 1 <= len(value) <= NAME_LENGTH
        or not all("!" <= character <= "~" for character in value)
    ):
        return RequestRejection.IDEMPOTENCY_KEY
    return value


def public_outcome(outcome: StartOutcome) -> AcceptedRun | SkippedRun:
    if isinstance(outcome, AcceptedStart):
        return AcceptedRun(
            outcome="accepted",
            run_id=RUN_PREFIX + outcome.run_id,
            graph_version_id=GRAPH_VERSION_PREFIX + outcome.graph_version_id,
            status="pending",
        )
    return SkippedRun(outcome="skipped", reason="overlap", run_id=None)


def _preparation(
    services: GraphServices, request: PreparedManualStartRequest
) -> StartPreparation:
    async def prepare(
        candidate: StartCandidate,
    ) -> StartAcceptance | StartFactsResult | None:
        async with services.preparer.reserve() as reservation:
            if reservation is None:
                return None
            selected = await acquire_manual_start(
                services.database, request, candidate, limits=IDENTITY
            )
            if isinstance(selected, StartAcceptance):
                return selected

            async def facts(_: Evaluator) -> StartFactsResult:
                return read_manual_start_facts(
                    selected, request, services.selection, limits=IDENTITY
                )

            return await reservation.run(facts)

    return prepare


def _response(
    result: StartResult, namespace: str, request_id: str
) -> ApiErrorEnvelope | Response:
    if result is None:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
    if isinstance(result, InstallationRefusal):
        return deployment_validation_error(result.diagnostics, request_id=request_id)
    if result is StartRefusal.PARAMETERS_UNAVAILABLE:
        return internal_error(request_id=request_id)
    if isinstance(result, StartRefusal):
        return request_rejection(REJECTIONS[result], request_id=request_id)
    outcome = result.outcome
    if outcome is AcceptanceOutcome.STALE:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
    if outcome in UNAVAILABLE_INTERPRETATIONS:
        return internal_error(request_id=request_id)
    if outcome in DEFINITIVE:
        return definitive_error(DEFINITIVE[outcome], request_id=request_id)
    if outcome is AcceptanceOutcome.FULL and result.limit and result.used is not None:
        return backlog_full_error(
            namespace, result.limit, result.used, request_id=request_id
        )
    if result.response is None:
        raise RuntimeError("A completed start carries its outcome.")
    created = outcome is AcceptanceOutcome.ACCEPTED
    return JSONResponse(
        public_outcome(result.response).model_dump(mode="json"),
        status_code=201 if created else 200,
    )


async def _trigger(
    services: GraphServices,
    clock: Clock,
    request: Request,
    principal: Principal,
    names: tuple[str, str],
) -> ApiErrorEnvelope | Response:
    request_id = current_exchange().request_id
    namespace_name, graph_name = names
    namespace_id = await authorized_namespace(
        services.database, principal, namespace_name
    )
    if namespace_id is None:
        return definitive_error(ApiErrorCode.FORBIDDEN, request_id=request_id)
    key = idempotency_key(request)
    if isinstance(key, RequestRejection):
        return request_rejection(key, request_id=request_id)
    try:
        body = await read_json_body(request, TriggerRequest, MAX_REQUEST_BYTES)
    except BodyRejected as rejected:
        return request_rejection(rejected.reason, request_id=request_id)

    async def graph_unit(connection: AsyncConnection) -> str | None:
        return await graph_id_by_name(connection, namespace_id, graph_name)

    graph_id = await services.database.read(graph_unit)
    if graph_id is None:
        return definitive_error(ApiErrorCode.NOT_FOUND, request_id=request_id)
    try:
        start = await asyncio.to_thread(
            prepare_manual_start_request,
            namespace_id,
            graph_id,
            dict(body.params),
            graph_version_id=None
            if body.graph_version is None
            else body.graph_version.removeprefix(GRAPH_VERSION_PREFIX),
            allow_unenforced_secrets=body.allow_unenforced_secrets,
        )
    except StartRecordRejected:
        return request_rejection(RequestRejection.PARAMETERS, request_id=request_id)
    result = await accept_manual_start(
        services.database,
        principal,
        start,
        services.identifiers.new() if key is None else key,
        _preparation(services, start),
        identifiers=services.identifiers,
        clock=clock.now,
    )
    return _response(result, namespace_name, request_id)


def trigger_handler(services: GraphServices, clock: Clock) -> TriggerHandler:
    async def trigger(request: Request, ns: str, graph: str) -> Response:
        principal = current_exchange().principal
        if principal is None:
            raise RuntimeError("A start requires an authenticated principal.")
        result = await _trigger(services, clock, request, principal, (ns, graph))
        if isinstance(result, ApiErrorEnvelope):
            return error_response(result, method=request.method)
        return result

    return trigger
