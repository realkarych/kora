from collections.abc import Awaitable, Callable
from typing import Literal

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


async def authenticate_route(
    request: Request,
    authenticator: Authenticator,
    clock: Clock,
    kind: Literal["user", "service"],
    *,
    current_authority: bool = False,
) -> Principal | ApiErrorEnvelope:
    info = request_info(request, current_authority)
    return await authenticate_personal_or_service(
        lambda ctx: authenticator.authenticate(info, ctx), clock, kind
    )


def request_info(request: Request, current_authority: bool) -> RequestInfo:
    headers = [
        value for name, value in request.headers.raw if name.lower() == b"authorization"
    ]
    return RequestInfo(
        headers[0].decode("latin-1") if len(headers) == 1 else None,
        requires_current_authority=current_authority,
    )


async def authenticate_personal_or_service(
    call: Callable[[AuthenticationContext], Awaitable[Principal]],
    clock: Clock,
    kind: Literal["user", "service"],
) -> Principal | ApiErrorEnvelope:
    exchange = current_exchange()
    try:
        principal = await call(AuthenticationContext(exchange.logger, clock))
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
    if principal.kind != kind:
        return definitive_error(ApiErrorCode.FORBIDDEN, request_id=exchange.request_id)
    return principal
