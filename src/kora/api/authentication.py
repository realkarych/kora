from starlette.requests import Request

from kora.api.errors import (
    DependencyRetryContext,
    InvalidCredential,
    definitive_error,
    dependency_unavailable,
    internal_error,
    unauthenticated_error,
)
from kora.api.exchange import current_exchange
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope
from kora.protocols.authentication import (
    AuthenticationContext,
    Authenticator,
    Principal,
    RequestInfo,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import Clock


async def schema_authorization(
    request: Request, authenticator: Authenticator, clock: Clock
) -> ApiErrorEnvelope | None:
    exchange = current_exchange()
    headers = request.headers.getlist("authorization")
    info = RequestInfo(
        headers[0] if len(headers) == 1 else None, requires_current_authority=True
    )
    try:
        principal = await authenticator.authenticate(
            info, AuthenticationContext(exchange.logger, clock)
        )
    except KoraProtocolError as error:
        if error.code == "authentication_rejected":
            return unauthenticated_error(
                InvalidCredential.UNKNOWN, request_id=exchange.request_id
            )
        if error.code == "authentication_unavailable":
            return dependency_unavailable(
                DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=exchange.request_id
            )
        exchange.failure(error)
        return internal_error(request_id=exchange.request_id)
    principal = Principal.model_validate(principal)
    if principal.kind != "user":
        return definitive_error(ApiErrorCode.FORBIDDEN, request_id=exchange.request_id)
    return None
