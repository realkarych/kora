from collections.abc import Callable

import pytest

from kora.cel import Code, Reason
from tests.cel.conformance.cases import read_cases, vendored_files
from tests.cel.support import accepted, rejected

NESTED_FORMS: dict[str, Callable[[int], str]] = {
    "addition": lambda levels: " + ".join(["a"] * (levels + 1)),
    "conjunction": lambda levels: " && ".join(["a"] * (levels + 1)),
    "selection": lambda levels: "a" + ".b" * levels,
    "index": lambda levels: "a" + "[0]" * levels,
    "conditional": lambda levels: "a ? b : " * levels + "c",
    "negation": lambda levels: "!" * levels + "x",
    "minus": lambda levels: "-" * levels + "x",
    "parentheses": lambda levels: "(" * levels + "x" + ")" * levels,
    "call_argument": lambda levels: "size(" * levels + "x" + ")" * levels,
    "list_literal": lambda levels: "[" * levels + "x" + "]" * levels,
    "map_literal": lambda levels: "{1: " * levels + "x" + "}" * levels,
}

PREFIX_FLOODS: dict[str, str] = {
    "parentheses": "(" * 4096,
    "conditionals": ("a ? b : " * 512)[:4096],
    "negations": "!" * 4096,
    "minus": "-" * 4096,
    "brackets": "[" * 4096,
    "braces": "{1: " * 1024,
}


@pytest.mark.parametrize("form", sorted(NESTED_FORMS))
def test_cel_rejects_expression_over_depth_limit(form: str) -> None:
    build = NESTED_FORMS[form]
    at_limit = build(32)
    over_limit = build(33)

    accepted(at_limit)
    diagnostic = rejected(over_limit)

    assert diagnostic.code is Code.LIMIT
    assert diagnostic.reason is Reason.DEPTH


@pytest.mark.parametrize("flood", sorted(PREFIX_FLOODS))
def test_cel_prefix_floods_stop_at_the_depth_limit(flood: str) -> None:
    source = PREFIX_FLOODS[flood]

    diagnostic = rejected(source)

    assert diagnostic.reason is Reason.DEPTH
    assert diagnostic.offset < 300


def test_cel_rejects_expression_over_length_limit() -> None:
    longest = "'" + "\U0001f600" * 4094 + "'"
    too_long = "'" + "a" * 4095 + "'"

    rendering = accepted(longest)
    diagnostic = rejected(too_long)

    assert len(longest) == 4096
    assert rendering.startswith('"\\U0001f600')
    assert diagnostic.code is Code.LIMIT
    assert diagnostic.reason is Reason.LENGTH
    assert diagnostic.offset == 4096


def test_cel_rejects_comprehension_over_limits() -> None:
    four_chained = "[1, 2].map(x, x).map(y, y).filter(z, true).exists(w, true)"
    two_nested = "[1].all(a, [2].all(b, true))"
    fifth = four_chained + " && [3].all(v, true)"
    three_nested = "[1].all(a, [2].all(b, [3].all(c, true)))"
    nested_in_receiver = "[1].map(a, a).map(b, [b].all(c, true))"

    accepted(four_chained)
    accepted(two_nested)
    accepted(nested_in_receiver)
    count = rejected(fifth)
    nesting = rejected(three_nested)

    assert count.code is Code.LIMIT
    assert count.reason is Reason.COMPREHENSION_COUNT
    assert nesting.code is Code.LIMIT
    assert nesting.reason is Reason.COMPREHENSION_NESTING
    assert three_nested[nesting.offset :].startswith("all(c")


def test_cel_depth_rule_matches_conformance_boundaries() -> None:
    parse_file = next(path for path in vendored_files() if path.stem == "parse")
    boundary_cases = [
        case
        for case in read_cases("parse", parse_file.read_text()).cases
        if case.section in ("nest", "repeat") and "TestAllTypes" not in case.expression
    ]
    mixed_at_limit = "(" * 16 + " + ".join(["a"] * 17) + ")" * 16
    mixed_over_limit = "(" * 17 + " + ".join(["a"] * 17) + ")" * 17

    for case in boundary_cases:
        accepted(case.expression)
    accepted(mixed_at_limit)
    diagnostic = rejected(mixed_over_limit)

    assert {case.name for case in boundary_cases} >= {"parens", "not", "unary_neg"}
    assert diagnostic.reason is Reason.DEPTH
