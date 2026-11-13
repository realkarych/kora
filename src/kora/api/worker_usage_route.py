from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from kora.api.errors import definitive_error, lock_timeout_error
from kora.api.exchange import current_exchange
from kora.api.responses import error_response
from kora.db.connection import Database, Dialect
from kora.db.identifiers import IdentifierGenerator
from kora.engine.attempt_usage import UsageAccess, UsageAccessRejected, report_usage
from kora.engine.graph_execution_contract import ExecutionContractReadLimits
from kora.engine.locks import TransitionLocks
from kora.engine.usage_acceptance import UsageAcceptance, UsageRefusal, UsageRejected
from kora.engine.usage_preparation import read_usage_context
from kora.models.errors import ApiError, ApiErrorCode, ApiErrorEnvelope, ConflictDetails
from kora.models.worker_usage import AcceptedUsageReport, AcceptedUsageRow, UsageReport
from kora.protocols.time import Clock


class WorkerUsageRoute:
    def __init__(
        self,
        database: Database,
        limits: ExecutionContractReadLimits,
        clock: Clock,
        identifiers: IdentifierGenerator,
        *,
        lock_timeout_ms: int,
    ) -> None:
        if type(lock_timeout_ms) is not int or lock_timeout_ms <= 0:
            raise ValueError("the usage lock timeout must be positive")
        self.database = database
        self.limits = limits
        self.clock = clock
        self.identifiers = identifiers
        self.lock_timeout_ms = lock_timeout_ms

    async def __call__(
        self, att: str, body: UsageReport
    ) -> AcceptedUsageReport | Response:
        exchange = current_exchange()
        principal = exchange.principal
        if principal is None:
            raise RuntimeError("worker authentication is required")
        try:
            context = await read_usage_context(
                self.database,
                att,
                body.dispatcher_id,
                body.lease_epoch,
                principal,
                self.clock,
                self.limits,
            )

            async def write(connection: AsyncConnection) -> UsageAcceptance:
                if self.database.dialect is Dialect.POSTGRESQL:
                    await connection.scalar(
                        select(
                            func.set_config(
                                "lock_timeout",
                                f"{self.lock_timeout_ms}ms",
                                True,
                            )
                        )
                    )
                locks = TransitionLocks(connection)
                await locks.acquire_run(context.run_id)
                return await report_usage(
                    connection,
                    locks,
                    context,
                    att,
                    body.dispatcher_id,
                    body.lease_epoch,
                    principal,
                    body.rows,
                    body.coverage,
                    self.clock,
                    self.identifiers,
                )

            accepted = await self.database.run(write)
        except UsageAccessRejected as error:
            codes = {
                UsageAccess.NOT_FOUND: ApiErrorCode.NOT_FOUND,
                UsageAccess.FORBIDDEN: ApiErrorCode.FORBIDDEN,
                UsageAccess.STALE_GRANT: ApiErrorCode.STALE_GRANT,
            }
            failure = definitive_error(
                codes[error.reason], request_id=exchange.request_id
            )
        except UsageRejected as error:
            failure = ApiErrorEnvelope(
                error=ApiError(
                    code=ApiErrorCode.CONFLICT,
                    message="The usage report conflicts with accepted accounting.",
                    retryable=False,
                    details=ConflictDetails(
                        reason=(
                            "usage_conflict"
                            if error.reason is UsageRefusal.CONFLICT
                            else "usage_regression"
                        )
                    ),
                    request_id=exchange.request_id,
                )
            )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            failure = lock_timeout_error(request_id=exchange.request_id)
        else:
            return AcceptedUsageReport(
                rows=tuple(
                    AcceptedUsageRow.model_validate(
                        item.row.model_dump() | {"usd_estimate": item.usd_estimate}
                    )
                    for item in accepted.accepted_rows
                ),
                coverage=accepted.coverage,
            )
        return error_response(failure, method="POST")
