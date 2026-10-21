import pytest

from kora.cel import Code, Reason
from tests.cel.support import accepted, rejected

REJECTED_TEXT: list[tuple[str, Reason]] = [
    ("\u0661 == 1", Reason.INVALID_CHARACTER),
    ("\u0430 == 1", Reason.INVALID_CHARACTER),
    ("x\u00a0== 1", Reason.INVALID_CHARACTER),
    ("x == '\u202e'", Reason.INVALID_CHARACTER),
    ("x == '\u2066a\u2069'", Reason.INVALID_CHARACTER),
    ("x\u200b== 1", Reason.INVALID_CHARACTER),
    ("x == 1 // \u202e\n", Reason.INVALID_CHARACTER),
    ("x == '\x07'", Reason.INVALID_CHARACTER),
    ("x == '\ud800'", Reason.INVALID_CHARACTER),
    ("x == '\ufeff'", Reason.INVALID_CHARACTER),
    ("a @ b", Reason.INVALID_CHARACTER),
    ("1 // c\r + 1", Reason.INVALID_CHARACTER),
    ("'''a\rb''' == x", Reason.INVALID_CHARACTER),
    ("r'''a\r\nb''' == x", Reason.INVALID_CHARACTER),
    ("b'''a\rb''' == x", Reason.INVALID_CHARACTER),
    ("b'\\u0041'", Reason.INVALID_ESCAPE),
    ("b'\\U0001F600'", Reason.INVALID_ESCAPE),
    ("0X10 == 16", Reason.UNEXPECTED_INPUT),
    ("x == 'a\U000e0041'", Reason.INVALID_CHARACTER),
    ("x == '\u0600'", Reason.INVALID_CHARACTER),
    ("x == '\U0001d173'", Reason.INVALID_CHARACTER),
    ("--9223372036854775808", Reason.INVALID_LITERAL),
    ("1_0 == 10", Reason.UNEXPECTED_INPUT),
    ("1e400", Reason.INVALID_LITERAL),
    ("9223372036854775808", Reason.INVALID_LITERAL),
    ("-9223372036854775809", Reason.INVALID_LITERAL),
    ("18446744073709551616u", Reason.INVALID_LITERAL),
    ("0x == 0", Reason.INVALID_LITERAL),
    ("'\\ud800'", Reason.INVALID_ESCAPE),
    ("'\\U00110000'", Reason.INVALID_ESCAPE),
    ("'\\q'", Reason.INVALID_ESCAPE),
    ("'\\x4'", Reason.INVALID_ESCAPE),
    ("'abc", Reason.UNTERMINATED_LITERAL),
    ("'a\nb'", Reason.UNTERMINATED_LITERAL),
]

ACCEPTED_TEXT: list[tuple[str, str]] = [
    ("-9223372036854775808", "-9223372036854775808"),
    ("18446744073709551615u", "18446744073709551615u"),
    ("0x1F == 31", "(31 == 31)"),
    ("'\\u202e' == x", '("\\u202e" == x)'),
    ("'''a\nb''' == x", '("a\\nb" == x)'),
    ("r'\\d+' == x", '("\\\\d+" == x)'),
    ("b'\\xff\\377' == x", '(b"\\xff\\xff" == x)'),
    ("x // note\n  + 1", "(x + 1)"),
    ("1.0e-400 == x", "(0.0 == x)"),
    (".5 + 1e3", "(0.5 + 1000.0)"),
    ("'\\101\\x41\\u0041' == x", '("AAA" == x)'),
    ("'\U0001f600' == x", '("\\U0001f600" == x)'),
    ("1 // c\r\n + 1", "(1 + 1)"),
    ("x // caf\u00e9\n + 1", "(x + 1)"),
    ("!-1", "(!(-1))"),
    ("--1", "1"),
    ("---1", "-1"),
    ("---9223372036854775808", "-9223372036854775808"),
    ("--x", "x"),
    ("---x", "(-x)"),
    ("-(-1)", "(-(-1))"),
    ("!!x", "x"),
    ("!!!x", "(!x)"),
    ("1\r+\r1", "(1 + 1)"),
    ("!!-1", "-1"),
    ("!!!-1", "(!(-1))"),
    ("x // a\fb\n + 1", "(x + 1)"),
    ("'caf\u00e9' == x", '("caf\\u00e9" == x)'),
]


@pytest.mark.parametrize(("source", "reason"), REJECTED_TEXT)
def test_cel_lexer_rejects_what_cel_rejects(source: str, reason: Reason) -> None:
    diagnostic = rejected(source)

    assert diagnostic.reason is reason
    assert diagnostic.code is Code.SYNTAX


@pytest.mark.parametrize(("source", "rendering"), ACCEPTED_TEXT)
def test_cel_lexer_accepts_cel_literals(source: str, rendering: str) -> None:
    result = accepted(source)

    assert result == rendering


def test_cel_reports_code_point_offsets() -> None:
    astral_then_fault = "'\U0001f600' == x @"
    astral_then_depth = "'\U0001f600\U0001f600' == " + "(" * 33 + "x" + ")" * 33

    invalid = rejected(astral_then_fault)
    deep = rejected(astral_then_depth)

    assert invalid.reason is Reason.INVALID_CHARACTER
    assert invalid.offset == 9
    assert deep.reason is Reason.DEPTH
    assert astral_then_depth[deep.offset] == "("
    assert deep.offset == 8 + 31
