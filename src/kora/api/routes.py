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
            if not isinstance(route, APIRoute):
                raise ValueError("The API handler inventory is invalid.")
            methods = route.methods
            if methods is None or methods != {"GET"}:
                raise ValueError("The API handler inventory is invalid.")
            routes.append(ActiveRoute(route, RouteName(route.path), frozenset(methods)))
        if len(routes) != 2 or {route.name for route in routes} != set(RouteName):
            raise ValueError("The API handler inventory is invalid.")
        self.routes = tuple(routes)

    def match(self, scope: Scope) -> ActiveRoute | None:
        for route in self.routes:
            matched, _ = route.route.matches(scope)
            if matched in (Match.FULL, Match.PARTIAL):
                return route
        return None
