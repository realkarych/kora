from starlette.responses import Response

from kora.api.errors import definitive_error
from kora.api.exchange import current_exchange
from kora.api.responses import error_response
from kora.engine.claim_context import ClaimAccessRejected, ClaimQuery
from kora.engine.worker_claim_poll import WorkerClaimPoll
from kora.models.errors import ApiErrorCode
from kora.models.worker_claims import WorkerClaimRequest
from kora.models.workers import WorkerName


class WorkerClaimsRoute:
    def __init__(self, poll: WorkerClaimPoll) -> None:
        self.poll = poll

    async def __call__(
        self, worker_id: WorkerName, body: WorkerClaimRequest
    ) -> Response:
        exchange = current_exchange()
        principal = exchange.principal
        if principal is None:
            raise RuntimeError("worker authentication is required")
        query = ClaimQuery(worker_id=worker_id, **body.model_dump(exclude={"max"}))
        try:
            delivery = await self.poll.poll(query, principal)
        except ClaimAccessRejected as error:
            code = {
                "forbidden": ApiErrorCode.FORBIDDEN,
                "conflict": ApiErrorCode.CONFLICT,
                "stale": ApiErrorCode.STALE_GRANT,
            }[error.reason]
            return error_response(
                definitive_error(code, request_id=exchange.request_id), method="POST"
            )
        return Response(
            b'{"claim":null}' if delivery is None else delivery.encode(),
            media_type="application/json",
        )
