import pytest

from kora.cel import Code, Reason
from kora.cel.regex import parse_pattern
from tests.cel.support import accepted, rejected

OUTSIDE_SUBSET: list[str] = [
    "(?x)a #b{1000}",
    "(?U)a+",
    "(?u)a",
    "(?R)a",
    "(?=a)",
    "(?!a)",
    "(?<=a)b",
    "(?<!a)b",
    "(a)\\\\1",
    "\\\\Qa\\\\E",
    "[[:alpha:]]",
    "[a&&b]",
    "[a--b]",
    "[a~~b]",
    "\\\\p{Unknown}",
    "\\\\p{^L}",
    "\\\\k<n>",
    "\\\\h",
    "\\\\y",
    "a\\\\<",
    "\\\\>a",
]

INVALID: list[str] = [
    "(",
    "a)",
    "a{,3}",
    "*a",
    "a{3,1}",
    "[a",
    "\\\\",
    "(?)",
    "a{x}",
    "(?i-)a",
    "(?-)a",
    "(?i-m-s)a",
    "(?P<1a>a)",
    "[\\\\d-z]",
    "(?P<a>x)(?P<a>y)",
    "(?P<a>x)(?<a>y)",
    "(?P<a>x)|(?P<a>y)",
]

SUPPORTED: list[str] = [
    "^[a-z0-9-]{1,63}$",
    "(?i)abc",
    "(?i:abc)def",
    "(?m)^a$",
    "(?s).+",
    "(?P<n>a)|(?<m>b)",
    "a*?b+?c??",
    "[[a][b]]",
    "[]a]",
    "[^]a]",
    "\\\\p{L}+\\\\P{Lu}\\\\pN",
    "\\\\x41\\\\x{1F600}",
    "a{2}{3}",
    "\\\\d\\\\D\\\\w\\\\W\\\\s\\\\S\\\\b\\\\B\\\\A\\\\z",
    "\\\\.\\\\*\\\\+\\\\?\\\\(\\\\)\\\\[\\\\]\\\\{\\\\}\\\\|\\\\^\\\\$\\\\#\\\\&\\\\-\\\\~",
    "",
    "(?i-m)a",
    "[\\\\d-]",
]

OVER_CAP: list[str] = [
    "a{1000}{1000}",
    "\\\\w{500}",
    "\\\\p{L}{500}",
    "\\\\W{500}",
    "\\\\P{L}{500}",
    "[\\\\p{L}\\\\d]{500}",
    "(?i)[a-z]{5000}",
    "(?i)a{20000}",
]


def matching(pattern: str) -> str:
    return f"x.matches('{pattern}')"


def test_cel_rejects_non_literal_regex_pattern() -> None:
    variable = rejected("x.matches(y)")
    concatenated = rejected("x.matches('a' + 'b')")
    raw = accepted("x.matches(r'\\d+')")

    assert variable.code is Code.SYNTAX
    assert variable.reason is Reason.NON_LITERAL_PATTERN
    assert concatenated.reason is Reason.NON_LITERAL_PATTERN
    assert raw == 'x.matches("\\\\d+")'


@pytest.mark.parametrize("pattern", OUTSIDE_SUBSET)
def test_cel_regex_rejects_constructs_outside_subset(pattern: str) -> None:
    diagnostic = rejected(matching(pattern))

    assert diagnostic.code is Code.SYNTAX
    assert diagnostic.reason is Reason.UNSUPPORTED_PATTERN


@pytest.mark.parametrize("pattern", INVALID)
def test_cel_regex_rejects_invalid_patterns(pattern: str) -> None:
    diagnostic = rejected(matching(pattern))

    assert diagnostic.reason is Reason.INVALID_PATTERN


@pytest.mark.parametrize("pattern", SUPPORTED)
def test_cel_regex_accepts_the_supported_subset(pattern: str) -> None:
    rendering = accepted(matching(pattern))

    assert rendering.startswith("x.matches(")


def test_cel_regex_compile_cost_bounded() -> None:
    groups_at_limit = "(" * 32 + "a" + ")" * 32
    groups_over_limit = "(" * 33 + "a" + ")" * 33
    classes_over_limit = "[" * 33 + "a" + "]" * 33
    flood = "(" * 4000

    accepted(matching(groups_at_limit))
    nested = rejected(matching(groups_over_limit))
    nested_classes = rejected(matching(classes_over_limit))
    flooded = rejected(matching(flood))
    oversized = [rejected(matching(pattern)) for pattern in OVER_CAP]

    assert nested.reason is Reason.PATTERN_NESTING
    assert nested_classes.reason is Reason.PATTERN_NESTING
    assert flooded.reason is Reason.PATTERN_NESTING
    assert all(diagnostic.code is Code.LIMIT for diagnostic in oversized)
    assert all(diagnostic.reason is Reason.PATTERN_SIZE for diagnostic in oversized)


def test_cel_regex_estimate_floors_wide_classes() -> None:
    any_character = parse_pattern(".")

    wide_range = parse_pattern("[\\x{0}-\\x{10FFFF}]")
    negated_class = parse_pattern("\\P{Z}")
    plus = parse_pattern("a+")
    counted = parse_pattern("a{1,}")
    star = parse_pattern("a*")

    assert wide_range >= any_character
    assert negated_class >= any_character
    assert plus == counted
    assert star < plus


@pytest.mark.parametrize("pattern", ["", "()" * 20, "|" * 31, "(?i)" * 16, "(?:)" * 16])
def test_cel_regex_charges_empty_structure_and_syntax(pattern: str) -> None:
    empty = parse_pattern("")

    weight = parse_pattern(pattern)

    assert empty > 0
    assert weight >= empty + len(pattern)
