from collections.abc import Callable

import pytest

from kora.cel import Reason
from kora.cel.diagnostics import DiagnosticError
from kora.cel.limits import MAX_RENDERING_DEPTH
from kora.cel.parser import RENDERING_LIMITS, parse
from kora.cel.render import RENDERING_EXPANSION, canonical_rendering
from tests.cel.conformance.cases import read_cases, vendored_files
from tests.cel.conformance.selection import rejection_for
from tests.cel.generator import generated_expressions

CORPUS: tuple[str, ...] = (
    "response.decision == 'approve'",
    "observation.status == 'merged'",
    "payload.commit == inputs.bundle.facts.change.commit",
    "inputs.verdict.approved == true && inputs.same.exit_code == 0",
    "inputs.verdict.comments",
    'inputs.review.verdict == "approved" && '
    "inputs.bench.cpu < inputs.baseline.cpu * 0.85",
    "response.decision == 'approve' && (!has(response.comments) || "
    "!response.comments.contains('block'))",
    "double(inputs.baseline.cpu) * 0.85",
    "inputs.gates.all(g, g.exit_code == 0)",
    "-1.f",
    "--19",
    "x - -1",
    "-(1)",
    "-(-1.5)",
    "a ? (b ? c : d) : e ? f : g",
    "1 + 2 * 3 - 4 / 5 % 6",
    "a in [1, 2, 3] && !(b in {'k': 1})",
    "'\\x00\\x7f\\u0080\\uffff\\U00010000'",
    "b'\\x00\\x7f\\x80\\xff\\'\\\"'",
    "b'\U0001f600\u4e2d\u00e9'",
    "!-1 || --1 == -(-1)",
)

NESTED_AT_LIMIT: dict[str, Callable[[], str]] = {
    "addition": lambda: " + ".join(["a"] * 33),
    "conditional": lambda: "a ? b : " * 32 + "c",
    "negation": lambda: "!" * 32 + "x",
    "minus_on_literal": lambda: "-" * 31 + "(1)",
    "negative_literal_under_minus": lambda: "-" * 32 + "1",
    "negation_of_negative_literal": lambda: "!" * 31 + "-1",
    "parenthesized_negation": lambda: "!(" * 16 + "x" + ")" * 16,
    "parenthesized_minus": lambda: "-(" * 16 + "1" + ")" * 16,
    "parenthesized_negative_literal": lambda: "-(" * 16 + "-1" + ")" * 16,
    "parenthesized_negation_of_negative": lambda: "!(" * 16 + "-1" + ")" * 16,
    "selection_of_negative": lambda: "(-1)" + ".b" * 31,
    "index": lambda: "a" + "[-1]" * 32,
    "call_argument": lambda: "size(" * 32 + "x" + ")" * 32,
    "list_of_binary": lambda: "[" * 16 + "a + " * 15 + "a" + "]" * 16,
    "map_values": lambda: "{1: " * 32 + "x" + "}" * 32,
    "macro_body": lambda: "l.all(v, " + " && ".join(["v"] * 32) + ")",
}


def bracket_depth(text: str) -> int:
    deepest = 0
    current = 0
    quote = ""
    escaped = False
    for character in text:
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in "'\"":
            quote = character
        elif character in "([{":
            current += 1
            deepest = max(deepest, current)
        elif character in ")]}":
            current -= 1
    return deepest


def conformance_corpus() -> list[str]:
    expressions: list[str] = []
    for path in vendored_files():
        for case in read_cases(path.stem, path.read_text()).cases:
            if rejection_for(case.file, case.section, case.name) is None:
                expressions.append(case.expression)
    return expressions


def test_cel_canonical_rendering_matches_author_text() -> None:
    corpus = [*CORPUS, *conformance_corpus(), *generated_expressions(20261020, 2000)]

    renderings = [(source, canonical_rendering(parse(source))) for source in corpus]

    for source, rendering in renderings:
        assert parse(rendering, RENDERING_LIMITS) == parse(source)
        assert rendering.isascii()
        assert len(rendering) <= RENDERING_EXPANSION * len(source)
    assert len(renderings) > 3000


@pytest.mark.parametrize("shape", sorted(NESTED_AT_LIMIT))
def test_cel_rendering_reparse_is_bounded(shape: str) -> None:
    source = NESTED_AT_LIMIT[shape]()

    tree = parse(source)
    rendering = canonical_rendering(tree)

    reparsed = parse(rendering, RENDERING_LIMITS)

    assert reparsed == tree
    assert bracket_depth(rendering) <= MAX_RENDERING_DEPTH


def test_cel_rendering_expansion_is_at_most_sixteen() -> None:
    source = "b'" + "\U0001f600" * 4093 + "'"

    rendering = canonical_rendering(parse(source))

    assert len(source) == 4096
    assert 15 * len(source) < len(rendering) <= RENDERING_EXPANSION * len(source)


def test_cel_rendering_beyond_the_reparse_bound_is_refused() -> None:
    rendering = "(" * (MAX_RENDERING_DEPTH + 1) + "x" + ")" * (MAX_RENDERING_DEPTH + 1)

    with pytest.raises(DiagnosticError) as raised:
        parse(rendering, RENDERING_LIMITS)

    assert raised.value.diagnostic.reason is Reason.DEPTH
