import asyncio
import math

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Scope

from kora.api.errors import definitive_error, internal_error
from kora.api.exchange import current_exchange
from kora.api.history_query import canonical_history_query, parse_history_query
from kora.api.responses import error_response
from kora.db.connection import Database, DatabaseSnapshotRejected
from kora.db.grant_history import HistoryReadLimits
from kora.engine.worker_history import (
    HistoryAccessRejected,
    HistoryRead,
    read_worker_history,
)
from kora.models.errors import ApiError, ApiErrorCode, ApiErrorEnvelope, ConflictDetails
from kora.models.worker_history import WorkerHistoryQuery
from kora.protocols.time import Clock


class WorkerHistoryRoute:
    def __init__(
        self,
        database: Database,
        limits: HistoryReadLimits,
        clock: Clock,
        *,
        worker_bytes: int,
        read_timeout: float,
    ) -> None:
        if not math.isfinite(read_timeout) or read_timeout <= 0:
            raise ValueError("the history read timeout must be positive and finite")
        self.database = database
        self.limits = limits
        self.clock = clock
        self.worker_bytes = worker_bytes
        self.read_timeout = read_timeout

    async def admit(self, scope: Scope) -> Response | None:
        deadline = asyncio.get_running_loop().time() + self.read_timeout
        try:
            query = await parse_history_query(scope.get("query_string", b""), deadline)
        except (ValueError, DatabaseSnapshotRejected) as error:
            failure = (
                internal_error(request_id=current_exchange().request_id)
                if isinstance(error, DatabaseSnapshotRejected)
                else definitive_error(
                    ApiErrorCode.VALIDATION,
                    request_id=current_exchange().request_id,
                )
            )
            return error_response(failure, method="GET")
        scope["query_string"] = canonical_history_query(query)
        scope["kora.history_query"] = query
        scope["kora.history_deadline"] = deadline
        return None

    async def __call__(self, att: str, request: Request) -> Response:
        exchange = current_exchange()
        principal = exchange.principal
        query = request.scope.get("kora.history_query")
        deadline = request.scope.get("kora.history_deadline")
        if (
            principal is None
            or not isinstance(query, WorkerHistoryQuery)
            or type(deadline) is not float
        ):
            raise RuntimeError("admitted worker history authority is required")
        try:
            encoded = await read_worker_history(
                self.database,
                HistoryRead(
                    att,
                    query.dispatcher_id,
                    query.lease_epoch,
                    query.tip(),
                    query.start(),
                ),
                principal,
                self.clock,
                self.limits,
                self.worker_bytes,
                deadline=deadline,
            )
        except HistoryAccessRejected as error:
            codes = {
                "not_found": ApiErrorCode.NOT_FOUND,
                "forbidden": ApiErrorCode.FORBIDDEN,
                "stale": ApiErrorCode.STALE_GRANT,
                "conflict": ApiErrorCode.CONFLICT,
            }
            failure = definitive_error(
                codes[error.reason], request_id=exchange.request_id
            )
            if error.reason == "conflict":
                failure = ApiErrorEnvelope(
                    error=ApiError(
                        code=ApiErrorCode.CONFLICT,
                        message=(
                            "Refresh the history descriptor "
                            "and compare the retained prefix."
                        ),
                        retryable=False,
                        details=ConflictDetails(reason="grant_history_conflict"),
                        request_id=exchange.request_id,
                    )
                )
            return error_response(failure, method="GET")
        return Response(encoded, media_type="application/json")
