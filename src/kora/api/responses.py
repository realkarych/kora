from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kora.api.errors import (
    HTTP_STATUSES,
    RequestRejection,
    error_metadata,
    internal_error,
    request_rejection,
)
from kora.api.exchange import current_exchange
from kora.api.routes import ActiveRoute
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope


def error_response(
    envelope: ApiErrorEnvelope, *, method: str, matched: ActiveRoute | None = None
) -> Response:
    code = envelope.error.code
    if code == ApiErrorCode.METHOD_NOT_ALLOWED:
        if matched is None:
            raise ValueError("Method refusal requires a matching active handler.")
        status = HTTP_STATUSES[code]
        headers = {"Allow": matched.allowed}
    else:
        metadata = error_metadata(envelope)
        status, headers = metadata.status, dict(metadata.headers)
    current_exchange().code = code.value
    if method == "HEAD":
        return Response(status_code=status, headers=headers)
    return JSONResponse(
        envelope.model_dump(mode="json"), status_code=status, headers=headers
    )


async def framework_error(request: Request, error: Exception) -> Response:
    exchange = current_exchange()
    envelope = request_rejection(
        RequestRejection.FRAMEWORK_SHAPE, request_id=exchange.request_id
    )
    return error_response(envelope, method=request.method)


async def validation_error(request: Request, error: Exception) -> Response:
    return await framework_error(request, error)


async def unexpected_error(request: Request, error: Exception) -> Response:
    exchange = current_exchange()
    exchange.failure(error)
    return error_response(
        internal_error(request_id=exchange.request_id), method=request.method
    )
