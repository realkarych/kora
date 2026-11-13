from collections.abc import Awaitable, Callable
from enum import StrEnum

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
    acquire_manual_start,
    commit_manual_start,
    select_manual_start,
)
from kora.engine.start_facts import (
    InstallationRefusal,
    ManualStartFacts,
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
START_ATTEMPTS = 2


class Preparation(StrEnum):
    BUSY = "busy"


StartResult = StartAcceptance | StartRefusal | InstallationRefusal | Preparation
DEFINITIVE: dict[AcceptanceOutcome, ApiErrorCode] = {
    AcceptanceOutcome.MISMATCHED: ApiErrorCode.IDEMPOTENCY_MISMATCH,
    AcceptanceOutcome.REFUSED: ApiErrorCode.FORBIDDEN,
    AcceptanceOutcome.MISSING: ApiErrorCode.NOT_FOUND,
}
REJECTIONS: dict[StartRefusal, RequestRejection] = {
    StartRefusal.PARAMETERS: RequestRejection.PARAMETERS,
    StartRefusal.SECRETS: RequestRejection.SECRETS,
}


def idempotency_key(request: Request) -> str | None | RequestRejection:
    value = request.headers.get("idempotency-key")
    if value is None:
        return None
    if not 1 <= len(value) <= NAME_LENGTH or not all(
        "!" <= character <= "~" for character in value
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


async def _prepared_start(
    services: GraphServices,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
) -> StartAcceptance | StartFactsResult | Preparation:
    candidate = await select_manual_start(services.database, principal, request, key)
    if isinstance(candidate, StartAcceptance):
        return candidate
    async with services.preparer.reserve() as reservation:
        if reservation is None:
            return Preparation.BUSY
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


async def _start(
    services: GraphServices,
    clock: Clock,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
) -> StartResult | None:
    for _ in range(START_ATTEMPTS):
        prepared = await _prepared_start(services, principal, request, key)
        if not isinstance(prepared, ManualStartFacts):
            return prepared
        committed = await commit_manual_start(
            services.database,
            principal,
            request,
            key,
            prepared,
            run_id=services.identifiers.new(),
            identifiers=services.identifiers,
            at=clock.now(),
        )
        if committed.outcome is not AcceptanceOutcome.STALE:
            return committed
    return None


def _response(
    result: StartResult | None, namespace: str, request_id: str
) -> ApiErrorEnvelope | Response:
    if result is None or result is Preparation.BUSY:
        return dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
    if isinstance(result, InstallationRefusal):
        return deployment_validation_error(result.diagnostics, request_id=request_id)
    if isinstance(result, StartRefusal):
        rejection = REJECTIONS.get(result)
        if rejection is None:
            return internal_error(request_id=request_id)
        return request_rejection(rejection, request_id=request_id)
    if result.outcome in DEFINITIVE:
        return definitive_error(DEFINITIVE[result.outcome], request_id=request_id)
    if result.outcome is AcceptanceOutcome.FULL:
        if result.limit is None or result.used is None:
            return internal_error(request_id=request_id)
        return backlog_full_error(
            namespace, result.limit, result.used, request_id=request_id
        )
    if result.response is None:
        return internal_error(request_id=request_id)
    created = result.outcome is AcceptanceOutcome.ACCEPTED
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
        start = prepare_manual_start_request(
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
    result = await _start(
        services,
        clock,
        principal,
        start,
        services.identifiers.new() if key is None else key,
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
