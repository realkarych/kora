import pytest

from kora.cel import Code, Diagnostic, Reason
from kora.cel.functions import (
    MACRO_NAMES,
    NARROWED_FUNCTIONS,
    STANDARD_FUNCTIONS,
    Form,
)
from tests.cel.support import accepted, checked_syntax, rejected

UNSUPPORTED: list[tuple[str, Reason]] = [
    ("Foo{}", Reason.MESSAGE_CONSTRUCTION),
    ("a.b.C{x: 1}", Reason.MESSAGE_CONSTRUCTION),
    ("a.?b", Reason.OPTIONAL_SYNTAX),
    ("a[?0]", Reason.OPTIONAL_SYNTAX),
    ("[?a]", Reason.OPTIONAL_SYNTAX),
    ("{?'k': v}", Reason.OPTIONAL_SYNTAX),
    (".a == 1", Reason.QUALIFIED_NAME),
    ("a.`b-c` == 1", Reason.QUOTED_FIELD),
    ("l.all(i, v, true)", Reason.TWO_VARIABLE_COMPREHENSION),
    ("l.exists(i, v, true)", Reason.TWO_VARIABLE_COMPREHENSION),
    ("l.exists_one(i, v, true)", Reason.TWO_VARIABLE_COMPREHENSION),
    ("l.filter(i, v, true)", Reason.TWO_VARIABLE_COMPREHENSION),
    ("if == 1", Reason.RESERVED_IDENTIFIER),
    ("while + 1", Reason.RESERVED_IDENTIFIER),
    ("x.in == 1", Reason.UNEXPECTED_INPUT),
    ("!-x", Reason.UNEXPECTED_INPUT),
    ("f(x,)", Reason.UNEXPECTED_INPUT),
    ("a ? b", Reason.UNEXPECTED_END),
    ("(a", Reason.UNEXPECTED_END),
]

UNKNOWN_OR_MALFORMED: list[tuple[str, Reason]] = [
    ("f(x)", Reason.UNKNOWN_FUNCTION),
    ("x.foo()", Reason.UNKNOWN_FUNCTION),
    ("existsOne(x)", Reason.UNKNOWN_FUNCTION),
    ("l.transformList(i, v, v)", Reason.UNKNOWN_FUNCTION),
    ("size(a, b)", Reason.UNKNOWN_FUNCTION),
    ("x.all(y)", Reason.MALFORMED_MACRO),
    ("x.map(y)", Reason.MALFORMED_MACRO),
    ("x.map(a, b, c, d)", Reason.MALFORMED_MACRO),
    ("x.all(1, true)", Reason.MALFORMED_MACRO),
    ("x.all(a.b, true)", Reason.MALFORMED_MACRO),
    ("has(x)", Reason.MALFORMED_MACRO),
    ("has(x[0])", Reason.MALFORMED_MACRO),
    ("has(a.b, c)", Reason.MALFORMED_MACRO),
]

STANDARD: list[str] = [
    "x.as == x.break",
    "x.map(a, a > 1, a * 2)",
    "has(a.b) && has(a.b.c)",
    "size(x) == x.size()",
    "t.getHours() + t.getDayOfWeek()",
    "string(x) + bytes(y) == dyn(z)",
    "type(x) == int",
    "x.matches('^a') && x.contains('b') && x.startsWith('c') && x.endsWith('d')",
    "timestamp(x) < timestamp(y) && duration(z) > duration(w)",
    "double(x) * 0.85 > double(uint(y))",
]


def narrowed_source(name: str, arity: int, form: Form) -> str:
    arguments = ", ".join("'a'" for _ in range(arity))
    if form is Form.GLOBAL:
        return f"{name}({arguments})"
    return f"x.{name}({arguments})"


@pytest.mark.parametrize(("source", "reason"), UNSUPPORTED)
def test_cel_rejects_unsupported_constructs(source: str, reason: Reason) -> None:
    diagnostic = rejected(source)

    assert diagnostic.code is Code.SYNTAX
    assert diagnostic.reason is reason


@pytest.mark.parametrize(("source", "reason"), UNKNOWN_OR_MALFORMED)
def test_cel_rejects_unknown_functions_and_malformed_macros(
    source: str, reason: Reason
) -> None:
    diagnostic = rejected(source)

    assert diagnostic.code is Code.SYNTAX
    assert diagnostic.reason is reason


@pytest.mark.parametrize(("name", "arity", "form"), NARROWED_FUNCTIONS)
def test_cel_rejects_functions_the_binding_lacks(
    name: str, arity: int, form: Form
) -> None:
    source = narrowed_source(name, arity, form)

    diagnostic = rejected(source)

    assert diagnostic.reason is Reason.UNKNOWN_FUNCTION


@pytest.mark.parametrize(
    ("name", "arity", "form"),
    [(function.name, function.arity, function.form) for function in STANDARD_FUNCTIONS],
)
def test_cel_accepts_every_table_entry(name: str, arity: int, form: Form) -> None:
    source = narrowed_source(name, arity, form)

    rendering = accepted(source)

    assert f"{name}(" in rendering


@pytest.mark.parametrize("macro", sorted(MACRO_NAMES))
def test_cel_accepts_every_macro_form(macro: str) -> None:
    two_arguments = f"l.{macro}(v, v == 1)"
    three_arguments = f"l.{macro}(v, v > 1, v * 2)"

    rendering = accepted(two_arguments)
    longer = checked_syntax(three_arguments)

    assert rendering == f"l.{macro}(v, (v == 1))"
    assert isinstance(longer, Diagnostic) == (macro != "map")


@pytest.mark.parametrize("source", STANDARD)
def test_cel_accepts_the_standard_functions_and_macros(source: str) -> None:
    rendering = accepted(source)

    assert rendering
