import asyncio
import math

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from starlette.responses import Response

from kora.api.errors import definitive_error, internal_error, lock_timeout_error
from kora.api.exchange import current_exchange
from kora.api.responses import error_response
from kora.db.connection import (
    Database,
    DatabaseWriterFailure,
    DatabaseWriterRejected,
    Dialect,
    complete,
)
from kora.db.grant_history import HistoryReadLimits
from kora.db.identifiers import IdentifierGenerator
from kora.engine.attempt_heartbeats import heartbeat_attempt
from kora.engine.claim_context import ClaimRun
from kora.engine.expiry_context import ExpiryContext
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.heartbeat_evidence import HeartbeatEvidenceRejected
from kora.engine.heartbeat_preparation import heartbeat_parent, lock_heartbeat_worker
from kora.engine.locks import TransitionLocks
from kora.engine.run_promotion_read import PromotionReadLimits
from kora.engine.usage_acceptance import UsageRefusal, UsageRejected
from kora.engine.worker_history import HistoryAccessRejected
from kora.models.errors import ApiError, ApiErrorCode, ApiErrorEnvelope, ConflictDetails
from kora.protocols.authentication import Principal
from kora.protocols.time import Clock
from kora.protocols.worker_heartbeats import HeartbeatResponse, WorkerHeartbeat


class WorkerHeartbeatRoute:
    def __init__(
        self,
        database: Database,
        limits: HistoryReadLimits,
        expiry_limits: PromotionReadLimits,
        clock: Clock,
        identifiers: IdentifierGenerator,
        *,
        worker_bytes: int,
        read_timeout: float,
        lock_timeout_ms: int,
    ) -> None:
        if not math.isfinite(read_timeout) or read_timeout <= 0:
            raise ValueError("the heartbeat read timeout must be positive and finite")
        if type(lock_timeout_ms) is not int or lock_timeout_ms <= 0:
            raise ValueError("the heartbeat lock timeout must be positive")
        self.database = database
        self.limits = limits
        self.expiry_limits = expiry_limits
        self.clock = clock
        self.identifiers = identifiers
        self.worker_bytes = worker_bytes
        self.read_timeout = read_timeout
        self.lock_timeout_ms = lock_timeout_ms

    async def _parent(self, att: str, principal: Principal) -> ClaimRun:
        self.limits.admit(0, 0, 0)
        deadline = asyncio.get_running_loop().time() + self.read_timeout
        async with self.database.read_snapshot(deadline=deadline) as reader:
            parent = await heartbeat_parent(reader, att, principal, self.limits)
        return parent

    async def _write(
        self,
        parent: ClaimRun,
        att: str,
        body: WorkerHeartbeat,
        principal: Principal,
    ) -> HeartbeatResponse:
        deadline = asyncio.get_running_loop().time() + self.lock_timeout_ms / 1000
        async with self.database.transaction(admission_deadline=deadline) as writer:
            if self.database.dialect is Dialect.POSTGRESQL:
                await writer.scalar(
                    select(
                        func.set_config(
                            "lock_timeout", f"{self.lock_timeout_ms}ms", True
                        )
                    )
                )
            locks = TransitionLocks(writer)
            authority = ExpiryContext(
                writer,
                locks,
                GraphResourceScope(
                    parent.namespace_id, parent.graph_id, parent.graph_version_id
                ),
                parent.id,
                self.clock.now,
                self.identifiers,
                self.expiry_limits,
                self.limits.attempt_row,
            )
            await locks.acquire_run(parent.id)
            await lock_heartbeat_worker(
                authority, principal, self.limits, self.worker_bytes
            )
            response = await heartbeat_attempt(
                authority,
                att,
                body,
                principal,
                self.limits,
                self.worker_bytes,
            )
        return response

    async def __call__(self, att: str, body: WorkerHeartbeat) -> Response:
        exchange = current_exchange()
        principal = exchange.principal
        if principal is None:
            raise RuntimeError("worker authentication is required")
        try:
            parent = await self._parent(att, principal)
            response = await complete(self._write(parent, att, body, principal))
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
                        code=failure.error.code,
                        message=failure.error.message,
                        retryable=failure.error.retryable,
                        details=ConflictDetails(reason="grant_history_conflict"),
                        request_id=failure.error.request_id,
                    )
                )
        except HeartbeatEvidenceRejected as error:
            failure = ApiErrorEnvelope(
                error=ApiError(
                    code=ApiErrorCode.VALIDATION
                    if error.details.reason == "evidence_unbound"
                    else ApiErrorCode.CONFLICT,
                    message="The heartbeat closure evidence was refused.",
                    retryable=False,
                    details=error.details,
                    request_id=exchange.request_id,
                )
            )
        except UsageRejected as error:
            failure = ApiErrorEnvelope(
                error=ApiError(
                    code=ApiErrorCode.CONFLICT,
                    message="The heartbeat conflicts with accepted usage coverage.",
                    retryable=False,
                    details=ConflictDetails(
                        reason="usage_conflict"
                        if error.reason is UsageRefusal.CONFLICT
                        else "usage_regression"
                    ),
                    request_id=exchange.request_id,
                )
            )
        except DatabaseWriterRejected as error:
            failure = (
                lock_timeout_error(request_id=exchange.request_id)
                if error.reason is DatabaseWriterFailure.DEADLINE
                else internal_error(request_id=exchange.request_id)
            )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            failure = lock_timeout_error(request_id=exchange.request_id)
        else:
            return Response(
                response.model_dump_json(
                    exclude={"publication"}
                    if response.grant_history is not None
                    else {"publication", "grant_history"}
                ),
                media_type="application/json",
            )
        return error_response(failure, method="POST")
