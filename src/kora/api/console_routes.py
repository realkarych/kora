import re
from asyncio import FIRST_COMPLETED, TaskGroup, sleep, wait
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from secrets import compare_digest

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import Scope

from kora.api.authentication import authenticate_personal_or_service, request_info
from kora.api.console_assets import ConsoleAssets
from kora.api.console_sessions import (
    SESSION_LIFETIME,
    ConsoleSession,
    ConsoleSessions,
    Credential,
)
from kora.api.errors import (
    DependencyRetryContext,
    definitive_error,
    dependency_unavailable,
)
from kora.api.exchange import current_exchange
from kora.api.graph_deployment_route import BodyRejected, read_json_body
from kora.api.responses import error_response
from kora.api.routes import CONSOLE_ROUTES, RouteName
from kora.api.run_event_route import RunEventRoute
from kora.models.console import (
    CODE_PATTERN,
    LoginLinkReceipt,
    LoginSelection,
)
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope
from kora.protocols.authentication import AuthenticationContext, Principal, RequestInfo

SESSION = "__Host-kora_session"
CSRF = "__Host-kora_csrf"
REDEMPTION = {
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "sec-fetch-user": "?1",
}
BROWSER_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
        b"connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'; "
        b"form-action 'none'",
    ),
)


def console_error(request: Request, code: ApiErrorCode) -> Response:
    request_id = current_exchange().request_id
    envelope = (
        dependency_unavailable(
            DependencyRetryContext.PROVEN_PRE_EFFECT, request_id=request_id
        )
        if code is ApiErrorCode.UNAVAILABLE
        else definitive_error(code, request_id=request_id)
    )
    return error_response(envelope, method=request.method)


def _secret(values: list[str]) -> str | None:
    if len(values) != 1:
        return None
    return values[0] if re.fullmatch(CODE_PATTERN, values[0]) else None


def _cookie(request: Request, key: str) -> str | None:
    pieces = (
        value.strip().partition("=")
        for header in request.headers.getlist("cookie")
        for value in header.split(";")
    )
    return _secret([value for name, equals, value in pieces if equals and name == key])


def _set_cookies(response: Response, session: str, csrf: str, max_age: int) -> None:
    set_cookie = partial(
        response.set_cookie, max_age=max_age, secure=True, samesite="strict"
    )
    set_cookie(SESSION, session, httponly=True)
    set_cookie(CSRF, csrf)


async def watch_redemption_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        await sleep(0)


@dataclass(frozen=True)
class ConsoleRoutes:
    origin: str
    assets: ConsoleAssets
    sessions: ConsoleSessions
    digest: Callable[[RequestInfo], str | None]
    authenticate_digest: Callable[[str, AuthenticationContext], Awaitable[Principal]]
    events_route: RunEventRoute

    def register(self, framework: FastAPI) -> None:
        selection = {"schema": LoginSelection.model_json_schema()}
        request_body = {"required": True, "content": {"application/json": selection}}
        for name, handler, status, schema in (
            (RouteName.LOGIN_LINKS, self.issue, 201, True),
            (RouteName.REDEEM_LOGIN, self.redeem, 303, True),
            (RouteName.CONSOLE_LOGOUT, self.logout, 204, True),
            (RouteName.CONSOLE_PAGE, self.page, 200, False),
            (RouteName.CONSOLE_ASSET, self.asset, 200, False),
            (RouteName.CONSOLE_EVENTS, self.events, 200, False),
        ):
            issuing = name is RouteName.LOGIN_LINKS
            framework.add_api_route(
                name.value,
                handler,
                methods=list(CONSOLE_ROUTES[name]),
                status_code=status,
                include_in_schema=schema,
                response_model=LoginLinkReceipt if issuing else None,
                openapi_extra={"requestBody": request_body} if issuing else None,
            )

    def admit(self, scope: Scope, name: RouteName) -> bool:
        path = scope["path"]
        raw = path.encode()
        served = name is not RouteName.CONSOLE_ASSET or path[1:] in self.assets.files
        return scope.get("raw_path", raw) == raw and served

    def correct_host(self, request: Request) -> bool:
        return request.scope.get("scheme") == "https" and request.headers.getlist(
            "host"
        ) == [self.origin.removeprefix("https://")]

    async def issue(self, request: Request) -> Response:
        digest = self.digest(request_info(request, True))
        principal = current_exchange().principal
        if digest is None or principal is None:
            return console_error(request, ApiErrorCode.UNAUTHENTICATED)
        try:
            selection = await read_json_body(request, LoginSelection, 1024)
        except BodyRejected:
            return console_error(request, ApiErrorCode.VALIDATION)
        credential = Credential(digest, principal.token_id, principal.owner)
        code = self.sessions.issue(selection, credential)
        if code is None:
            return console_error(request, ApiErrorCode.UNAVAILABLE)
        url = self.origin + RouteName.REDEEM_LOGIN.value.format(code=code)
        return JSONResponse({"url": url}, status_code=201)

    async def authority(
        self, credential: Credential
    ) -> Principal | ApiErrorEnvelope | None:
        principal = await authenticate_personal_or_service(
            lambda ctx: self.authenticate_digest(credential.digest, ctx),
            self.sessions.clock,
            "user",
        )
        if isinstance(principal, Principal) and (
            principal.token_id,
            principal.owner,
        ) != (credential.token_id, credential.owner):
            return None
        return principal

    async def redeem(self, code: str, request: Request) -> Response:
        if (
            any(
                request.headers.getlist(key) != [value]
                for key, value in REDEMPTION.items()
            )
            or "sec-purpose" in request.headers
            or "purpose" in request.headers
        ):
            return console_error(request, ApiErrorCode.FORBIDDEN)
        if re.fullmatch(CODE_PATTERN, code) is None:
            return console_error(request, ApiErrorCode.UNAUTHENTICATED)
        lengths = request.headers.getlist("content-length")
        if lengths not in ([], ["0"]) or "transfer-encoding" in request.headers:
            return console_error(request, ApiErrorCode.VALIDATION)
        with self.sessions.reserve(code) as link:
            if link is None:
                return console_error(request, ApiErrorCode.UNAUTHENTICATED)
            async with TaskGroup() as checks:
                authority = checks.create_task(self.authority(link.credential))
                closed = checks.create_task(watch_redemption_disconnect(request))
                await wait((authority, closed), return_when=FIRST_COMPLETED)
                if closed.done():
                    authority.cancel()
                    return console_error(request, ApiErrorCode.UNAUTHENTICATED)
                closed.cancel()
            principal = authority.result()
            if isinstance(principal, ApiErrorEnvelope):
                return error_response(principal, method=request.method)
            now = self.sessions.clock.now()
            if principal is None or link.expires <= now:
                return console_error(request, ApiErrorCode.UNAUTHENTICATED)
            started = self.sessions.start(link, now)
            if started is None:
                return console_error(request, ApiErrorCode.UNAVAILABLE)
            key, session = started
            selection = link.selection
            target = f"/console/runs/{selection.namespace}/{selection.run_id}"
            response = RedirectResponse(target, status_code=303)
            max_age = int(SESSION_LIFETIME.total_seconds())
            _set_cookies(response, key, session.csrf, max_age)
            return response

    async def page(self) -> Response:
        return Response(self.assets.html, media_type="text/html")

    async def asset(self, asset: str) -> Response:
        media_type, body = self.assets.files[f"assets/{asset}"]
        return Response(body, media_type=media_type)

    def session(self, request: Request) -> tuple[str, ConsoleSession] | None:
        key = _cookie(request, SESSION)
        session = self.sessions.current(key)
        return None if key is None or session is None else (key, session)

    async def logout(self, request: Request) -> Response:
        if request.headers.getlist("origin") != [self.origin]:
            return console_error(request, ApiErrorCode.FORBIDDEN)
        active = self.session(request)
        if active is None:
            return console_error(request, ApiErrorCode.UNAUTHENTICATED)
        key, session = active
        csrf = _cookie(request, CSRF)
        header = _secret(request.headers.getlist("x-csrf-token"))
        if (
            csrf is None
            or header is None
            or not compare_digest(header, csrf)
            or not compare_digest(csrf, session.csrf)
        ):
            return console_error(request, ApiErrorCode.FORBIDDEN)
        del self.sessions.sessions[key]
        response = Response(status_code=204)
        _set_cookies(response, "", "", 0)
        return response

    async def events(self, ns: str, run: str, request: Request) -> Response:
        if request.headers.getlist("sec-fetch-site") != [
            "same-origin"
        ] or request.headers.getlist("origin") not in ([], [self.origin]):
            return console_error(request, ApiErrorCode.FORBIDDEN)
        exchange = current_exchange()
        exchange.authorize_run_events = partial(self.authorize_events, request)
        return await self.events_route(ns, run, request)

    async def authorize_events(self, request: Request) -> Response | None:
        active = self.session(request)
        if active is None:
            return console_error(request, ApiErrorCode.UNAUTHENTICATED)
        key, session = active
        authority = await self.authority(session.credential)
        if isinstance(authority, ApiErrorEnvelope):
            if authority.error.code in (
                ApiErrorCode.UNAUTHENTICATED,
                ApiErrorCode.FORBIDDEN,
            ):
                self.sessions.sessions.pop(key, None)
            return error_response(authority, method=request.method)
        if authority is None:
            self.sessions.sessions.pop(key, None)
        if authority is None or self.sessions.current(key) is not session:
            return console_error(request, ApiErrorCode.UNAUTHENTICATED)
        current_exchange().principal = authority
        return None
