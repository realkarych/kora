import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from kora.cel.cost import Estimate, Variable, estimate
from kora.cel.diagnostics import Diagnostic, DiagnosticError
from kora.cel.meter import Meter
from kora.cel.parser import parse
from kora.cel.paths import Path, parse_path
from kora.cel.render import canonical_rendering
from kora.cel.tree import Node


class Seal:
    pass


SEAL: Final[Seal] = Seal()


@dataclass(frozen=True)
class CheckedExpression:
    tree: Node
    rendering: str
    digest: str
    estimate: Estimate
    seal: Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not SEAL:
            raise TypeError("checked expressions come only from kora.cel.check")

    @property
    def variables(self) -> tuple[str, ...]:
        return self.estimate.variables


def check(
    source: str,
    environment: Mapping[str, Variable] | None = None,
    meter: Meter | None = None,
) -> CheckedExpression | Diagnostic:
    used = meter if meter is not None else Meter()
    try:
        tree = parse(source, meter=used)
        rendering = canonical_rendering(tree)
        cost = estimate(tree, len(rendering), environment or {}, meter=used)
    except DiagnosticError as error:
        return error.diagnostic
    digest = hashlib.sha256(rendering.encode("ascii")).hexdigest()
    return CheckedExpression(tree, rendering, digest, cost, SEAL)


def check_path(source: str) -> Path | Diagnostic:
    try:
        return parse_path(source)
    except DiagnosticError as error:
        return error.diagnostic
