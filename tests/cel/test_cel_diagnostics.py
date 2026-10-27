import pytest

from kora.cel import (
    Code,
    Diagnostic,
    Environment,
    Reason,
    ResultKind,
    Variable,
    check,
    check_typed,
)
from kora.cel.diagnostics import DiagnosticError
from kora.cel.parser import parse
from kora.cel.types import INT, STRING, Kind, Primitive, Record, RecordField

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


MARKED_FORM: Record = Record(
    {
        "decision": RecordField(Primitive(Kind.STRING, frozenset({MARKER})), True),
        "optional": RecordField(STRING, False),
        "text": RecordField(STRING, True),
    }
)
MARKED_ENVIRONMENT: Environment = Environment(
    {
        "response": Variable(MARKED_FORM),
        "inputs": Variable(Record({IDENTIFIER_MARKER: RecordField(INT, True)})),
    },
    ResultKind.BOOL,
    frozenset({"inputs"}),
)
TYPED_SOURCES: dict[Reason, str] = {
    Reason.UNKNOWN_IDENTIFIER: f"{IDENTIFIER_MARKER} == 1",
    Reason.ROOT_NOT_SELECTED: f"inputs == {{'{MARKER}': 1}}",
    Reason.UNKNOWN_FIELD: f"response.{IDENTIFIER_MARKER} == '{MARKER}'",
    Reason.SHADOWED_VARIABLE: f"['{MARKER}'].all(response, true)",
    Reason.UNKNOWN_ENUM_VALUE: f"response.decision == '{MARKER}-2'",
    Reason.NO_MATCHING_OVERLOAD: f"'{MARKER}' + 1 == '{MARKER}'",
    Reason.UNGUARDED_OPTIONAL: f"response.optional == '{MARKER}'",
    Reason.STRING_SIZE: f"size('{MARKER}') > 0",
    Reason.REPEATED_MAP_KEY: f"{{'{MARKER}': 1, '{MARKER}': 2}}.size() > 0",
    Reason.WRONG_RESULT_TYPE: f"inputs.{IDENTIFIER_MARKER}",
}


@pytest.mark.parametrize(
    "reason", list(TYPED_SOURCES), ids=[reason.value for reason in TYPED_SOURCES]
)
def test_cel_checker_diagnostics_exclude_author_text(reason: Reason) -> None:
    source = TYPED_SOURCES[reason]

    result = check_typed(source, MARKED_ENVIRONMENT)

    assert isinstance(result, Diagnostic)
    assert (result.reason, result.code) == (reason, Code.SYNTAX)
    for text in (result.message, repr(result), str(result)):
        assert MARKER not in text
        assert IDENTIFIER_MARKER not in text
