import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from kora.cel.checker import Environment, check_types
from kora.cel.cost import Estimate, Variable, check_environment, estimate
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


def check_typed(
    source: str, environment: Environment, meter: Meter | None = None
) -> CheckedExpression | Diagnostic:
    used = meter if meter is not None else Meter()
    try:
        check_environment(environment.variables)
        typed = check_types(parse(source, meter=used), environment, used)
        rendering = canonical_rendering(typed.tree, used)
        cost = estimate(typed.tree, len(rendering), environment.variables, meter=used)
    except DiagnosticError as error:
        return error.diagnostic
    digest = hashlib.sha256(rendering.encode("ascii")).hexdigest()
    return CheckedExpression(SEAL, typed.tree, rendering, digest, cost, typed.paths)


def check_path(source: str) -> Path | Diagnostic:
    try:
        return parse_path(source)
    except DiagnosticError as error:
        return error.diagnostic
