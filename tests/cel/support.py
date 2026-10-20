from kora.cel import Diagnostic
from kora.cel.diagnostics import DiagnosticError
from kora.cel.parser import parse
from kora.cel.render import canonical_rendering
from kora.cel.tree import Node


def checked_syntax(source: str) -> tuple[Node, str] | Diagnostic:
    try:
        tree = parse(source)
        return tree, canonical_rendering(tree)
    except DiagnosticError as error:
        return error.diagnostic


def rejected(source: str) -> Diagnostic:
    result = checked_syntax(source)
    assert isinstance(result, Diagnostic), f"accepted: {source[:80]!r}"
    return result


def accepted(source: str) -> str:
    result = checked_syntax(source)
    assert not isinstance(result, Diagnostic), f"rejected {result}: {source[:80]!r}"
    return result[1]
