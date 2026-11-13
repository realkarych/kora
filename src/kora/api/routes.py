from dataclasses import dataclass
from enum import StrEnum

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

RECOGNIZED_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


class RouteName(StrEnum):
    HEALTH = "/healthz"
    SCHEMA = "/api/v1/openapi.json"
    GRAPH_VERSIONS = "/api/v1/namespaces/{ns}/graphs/{graph}/versions"
    GRAPH_RUNS = "/api/v1/namespaces/{ns}/graphs/{graph}/runs"
    WORKERS = "/api/v1/workers"


ROUTE_METHODS: dict[RouteName, frozenset[str]] = {
    RouteName.HEALTH: frozenset({"GET"}),
    RouteName.SCHEMA: frozenset({"GET"}),
    RouteName.GRAPH_VERSIONS: frozenset({"POST"}),
    RouteName.GRAPH_RUNS: frozenset({"POST"}),
    RouteName.WORKERS: frozenset({"POST"}),
}
REQUIRED_ROUTES: frozenset[RouteName] = frozenset({RouteName.HEALTH, RouteName.SCHEMA})
PROTECTED_ROUTES: frozenset[RouteName] = frozenset(
    {
        RouteName.SCHEMA,
        RouteName.GRAPH_VERSIONS,
        RouteName.GRAPH_RUNS,
        RouteName.WORKERS,
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
