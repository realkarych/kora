import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from kora.cel.checker import Environment, check_types
from kora.cel.cost import (
    Estimate,
    PreparedBounds,
    Variable,
    check_environment,
    estimate,
)
from kora.cel.diagnostics import Diagnostic, DiagnosticError
from kora.cel.meter import Meter
from kora.cel.parser import parse
from kora.cel.paths import Path, parse_path
from kora.cel.projection import PathSet
from kora.cel.render import canonical_rendering
from kora.cel.tree import Node


class CheckedExpression:
    __slots__ = ("_digest", "_estimate", "_paths", "_rendering", "_tree")

    def __init__(
        self,
        seal: object,
        tree: Node,
        rendering: str,
        digest: str,
        estimate: Estimate,
        paths: Mapping[str, PathSet] | None = None,
    ) -> None:
        if seal is not SEAL:
            raise TypeError("checked expressions come only from kora.cel.check")
        self._tree: Node = tree
        self._rendering: str = rendering
        self._digest: str = digest
        self._estimate: Estimate = estimate
        self._paths: Mapping[str, PathSet] = MappingProxyType(dict(paths or {}))

    @property
    def tree(self) -> Node:
        return self._tree

    @property
    def rendering(self) -> str:
        return self._rendering

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def estimate(self) -> Estimate:
        return self._estimate

    @property
    def variables(self) -> tuple[str, ...]:
        return self._estimate.variables

    @property
    def paths(self) -> Mapping[str, PathSet]:
        return self._paths


SEAL: Final[object] = object()


def check(
    source: str,
    environment: Mapping[str, Variable] | None = None,
    meter: Meter | None = None,
) -> CheckedExpression | Diagnostic:
    used = meter if meter is not None else Meter()
    variables: Mapping[str, Variable] = {} if environment is None else environment
    try:
        tree = parse(source, meter=used)
        rendering = canonical_rendering(tree, used)
        cost = estimate(tree, len(rendering), variables, meter=used)
    except DiagnosticError as error:
        return error.diagnostic
    digest = hashlib.sha256(rendering.encode("ascii")).hexdigest()
    return CheckedExpression(SEAL, tree, rendering, digest, cost)


class PreparedEnvironment:
    __slots__ = ("_bounds", "_environment")

    def __init__(
        self, seal: object, environment: Environment, bounds: PreparedBounds
    ) -> None:
        if seal is not SEAL:
            raise TypeError("prepared environments come only from kora.cel.prepare")
        self._environment: Environment = environment
        self._bounds: PreparedBounds = bounds

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def bounds(self) -> PreparedBounds:
        return self._bounds


def prepare(
    environment: Environment, meter: Meter | None = None
) -> PreparedEnvironment:
    used = meter if meter is not None else Meter()
    check_environment(environment.variables)
    snapshot = Environment(
        dict(environment.variables), environment.result, environment.selection_only
    )
    return PreparedEnvironment(SEAL, snapshot, PreparedBounds(snapshot.variables, used))


def check_typed(
    source: str,
    environment: Environment | PreparedEnvironment,
    meter: Meter | None = None,
) -> CheckedExpression | Diagnostic:
    used = meter if meter is not None else Meter()
    plain: Environment
    bounds: PreparedBounds | None
    if isinstance(environment, PreparedEnvironment):
        plain, bounds = environment.environment, environment.bounds
    else:
        plain, bounds = environment, None
    validated = frozenset[str]() if bounds is None else bounds.validated
    try:
        check_environment(plain.variables)
        typed = check_types(parse(source, meter=used), plain, used, validated)
        rendering = canonical_rendering(typed.tree, used)
        cost = estimate(
            typed.tree,
            len(rendering),
            plain.variables,
            meter=used,
            prepared=bounds,
        )
    except DiagnosticError as error:
        return error.diagnostic
    digest = hashlib.sha256(rendering.encode("ascii")).hexdigest()
    return CheckedExpression(SEAL, typed.tree, rendering, digest, cost, typed.paths)


def check_path(source: str) -> Path | Diagnostic:
    try:
        return parse_path(source)
    except DiagnosticError as error:
        return error.diagnostic
