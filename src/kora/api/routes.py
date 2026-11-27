from dataclasses import dataclass
from enum import StrEnum

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

from kora.models.console import LOGIN_LINK_PATH, REDEEM_PATH

RECOGNIZED_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


class RouteName(StrEnum):
    HEALTH = "/healthz"
    SCHEMA = "/api/v1/openapi.json"
    GRAPH_VERSIONS = "/api/v1/namespaces/{ns}/graphs/{graph}/versions"
    GRAPH_RUNS = "/api/v1/namespaces/{ns}/graphs/{graph}/runs"
    RUN_EVENTS = "/api/v1/namespaces/{ns}/runs/{run}/events"
    LOGIN_LINKS = LOGIN_LINK_PATH
    REDEEM_LOGIN = REDEEM_PATH
    CONSOLE_LOGOUT = "/api/v1/auth/session"
    CONSOLE_PAGE = "/console/runs/{ns}/{run}"
    CONSOLE_ASSET = "/assets/{asset}"
    CONSOLE_EVENTS = "/console/api/v1/namespaces/{ns}/runs/{run}/events"
    WORKERS = "/api/v1/workers"
    WORKER_CLAIMS = "/api/v1/workers/{worker_id:path}/claims"
    WORKER_HISTORY = "/api/v1/attempts/{att}/grants"
    USAGE = "/api/v1/attempts/{att}/usage"
    HEARTBEAT = "/api/v1/attempts/{att}/heartbeat"


CONSOLE_ROUTES: dict[RouteName, frozenset[str]] = {
    RouteName.LOGIN_LINKS: frozenset({"POST"}),
    RouteName.REDEEM_LOGIN: frozenset({"GET"}),
    RouteName.CONSOLE_LOGOUT: frozenset({"DELETE"}),
    RouteName.CONSOLE_PAGE: frozenset({"GET"}),
    RouteName.CONSOLE_ASSET: frozenset({"GET"}),
    RouteName.CONSOLE_EVENTS: frozenset({"GET"}),
}
ROUTE_METHODS: dict[RouteName, frozenset[str]] = {
    RouteName.HEALTH: frozenset({"GET"}),
    RouteName.SCHEMA: frozenset({"GET"}),
    RouteName.GRAPH_VERSIONS: frozenset({"POST"}),
    RouteName.GRAPH_RUNS: frozenset({"POST"}),
    RouteName.RUN_EVENTS: frozenset({"GET"}),
    RouteName.WORKERS: frozenset({"POST"}),
    RouteName.WORKER_CLAIMS: frozenset({"POST"}),
    RouteName.WORKER_HISTORY: frozenset({"GET"}),
    RouteName.USAGE: frozenset({"POST"}),
    RouteName.HEARTBEAT: frozenset({"POST"}),
    **CONSOLE_ROUTES,
}
REQUIRED_ROUTES: frozenset[RouteName] = frozenset({RouteName.HEALTH, RouteName.SCHEMA})
PROTECTED_ROUTES: frozenset[RouteName] = frozenset(
    {
        RouteName.SCHEMA,
        RouteName.GRAPH_VERSIONS,
        RouteName.GRAPH_RUNS,
        RouteName.RUN_EVENTS,
        RouteName.LOGIN_LINKS,
        RouteName.WORKERS,
        RouteName.WORKER_CLAIMS,
        RouteName.WORKER_HISTORY,
        RouteName.USAGE,
        RouteName.HEARTBEAT,
    }
)


@dataclass(frozen=True)
class ActiveRoute:
    route: APIRoute
    name: RouteName
    methods: frozenset[str]

    @property
    def allowed(self) -> str:
        return ", ".join(sorted(self.methods))


class ActiveCatalog:
    def __init__(self, application: FastAPI) -> None:
        routes: list[ActiveRoute] = []
        for route in application.routes:
            if not isinstance(route, APIRoute) or route.path not in set(RouteName):
                raise ValueError("The API handler inventory is invalid.")
            name = RouteName(route.path)
            methods = frozenset(route.methods or ())
            if methods != ROUTE_METHODS[name]:
                raise ValueError("The API handler inventory is invalid.")
            routes.append(ActiveRoute(route, name, methods))
        names = {route.name for route in routes}
        if len(names) != len(routes) or not REQUIRED_ROUTES <= names:
            raise ValueError("The API handler inventory is invalid.")
        self.routes = tuple(routes)

    def match(self, scope: Scope) -> ActiveRoute | None:
        for route in self.routes:
            matched, _ = route.route.matches(scope)
            if matched in (Match.FULL, Match.PARTIAL):
                return route
        return None
