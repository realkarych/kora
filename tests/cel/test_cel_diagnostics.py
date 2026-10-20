import pytest

from kora.cel import Diagnostic, check
from kora.cel.diagnostics import DiagnosticError
from kora.cel.parser import parse

MARKER = "UNREGISTERED-MARKER-7731"
IDENTIFIER_MARKER = "unregistered_marker_7731"

SOURCES: list[str] = [
    f"{IDENTIFIER_MARKER} @",
    f"{IDENTIFIER_MARKER}.{IDENTIFIER_MARKER}()",
    f"'{MARKER}' == '{MARKER}",
    f"x.matches('{MARKER}(')",
    f"x.matches('(?x){MARKER}')",
    f"{IDENTIFIER_MARKER}{{a: 1}}",
    f"'{MARKER}\\q'",
    f"{IDENTIFIER_MARKER} + " * 40 + "1",
    f"[{IDENTIFIER_MARKER}].all(a, [a].all(b, [b].all(c, true)))",
]


@pytest.mark.parametrize("source", SOURCES)
def test_cel_diagnostics_exclude_author_text(source: str) -> None:
    result = check(source)
    with pytest.raises(DiagnosticError) as raised:
        parse(source)

    assert isinstance(result, Diagnostic)
    texts = [result.message, repr(result), str(raised.value), repr(raised.value.args)]
    for text in texts:
        assert MARKER not in text
        assert IDENTIFIER_MARKER not in text
    assert raised.value.__cause__ is None
