import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from kora.cel.checker import Environment, check_types
from kora.cel.cost import (
    Estimate,
    PreparedCost,
    ValueBound,
    Variable,
    check_environment,
    estimate,
    prepare_cost,
)
from kora.cel.diagnostics import Diagnostic, DiagnosticError
from kora.cel.meter import Meter
from kora.cel.parser import parse
from kora.cel.paths import Path, parse_path
from kora.cel.projection import PathSet, projection_bytes
from kora.cel.render import canonical_rendering
from kora.cel.tree import Node


class CheckedExpression:
    __slots__ = (
        "_bounds",
        "_digest",
        "_estimate",
        "_rendering",
        "_tree",
        "_projections",
        "_projection_bytes",
    )

    def __init__(
        self,
        seal: object,
        tree: Node,
        rendering: str,
        digest: str,
        estimate: Estimate,
        bounds: Mapping[str, ValueBound],
        projections: Mapping[str, PathSet] | None = None,
    ) -> None:
        if seal is not SEAL:
            raise TypeError("checked expressions come only from kora.cel.check")
        self._tree: Node = tree
        self._rendering: str = rendering
        self._digest: str = digest
        self._estimate: Estimate = estimate
        self._bounds: Mapping[str, ValueBound] = MappingProxyType(dict(bounds))
        self._projections: Mapping[str, PathSet] = MappingProxyType(
            {} if projections is None else dict(projections)
        )
        self._projection_bytes: int = projection_bytes(self._projections)

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
    def bounds(self) -> Mapping[str, ValueBound]:
        return self._bounds

    @property
    def bound_bytes(self) -> int:
        return self._estimate.bound_bytes

    @property
    def projections(self) -> Mapping[str, PathSet]:
        return self._projections

    @property
    def projection_bytes(self) -> int:
        return self._projection_bytes


SEAL: Final[object] = object()


class PreparedEnvironment:
    __slots__ = ("_cost", "_environment")

    def __init__(
        self, seal: object, environment: Environment, cost: PreparedCost
    ) -> None:
        if seal is not SEAL:
            raise TypeError("prepared environments come only from kora.cel.prepare")
        self._environment: Environment = environment
        self._cost: PreparedCost = cost

    def check(self, source: str, meter: Meter) -> CheckedExpression | Diagnostic:
        return typed_check(source, self._environment, meter, self._cost)


def prepare(
    environment: Environment, meter: Meter | None = None
) -> PreparedEnvironment:
    used = meter if meter is not None else Meter()
    cost = prepare_cost(environment.variables, used)
    snapshot = Environment(
        dict(environment.variables), environment.result, environment.selection_only
    )
    return PreparedEnvironment(SEAL, snapshot, cost)


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
    return CheckedExpression(SEAL, tree, rendering, digest, cost, cost.bounds)


def check_typed(
    source: str,
    environment: Environment | PreparedEnvironment,
    meter: Meter | None = None,
) -> CheckedExpression | Diagnostic:
    used = meter if meter is not None else Meter()
    if isinstance(environment, PreparedEnvironment):
        return environment.check(source, used)
    return typed_check(source, environment, used)


def typed_check(
    source: str,
    environment: Environment,
    used: Meter,
    prepared: PreparedCost | None = None,
) -> CheckedExpression | Diagnostic:
    try:
        if prepared is None:
            check_environment(environment.variables)
        tree, projections = check_types(
            parse(source, meter=used), environment, used, prepared is not None
        )
        rendering = canonical_rendering(tree, used)
        cost = estimate(
            tree, len(rendering), environment.variables, meter=used, prepared=prepared
        )
    except DiagnosticError as error:
        return error.diagnostic
    digest = hashlib.sha256(rendering.encode("ascii")).hexdigest()
    return CheckedExpression(
        SEAL, tree, rendering, digest, cost, cost.bounds, projections
    )


def check_path(source: str) -> Path | Diagnostic:
    try:
        return parse_path(source)
    except DiagnosticError as error:
        return error.diagnostic
