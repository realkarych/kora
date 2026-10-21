from dataclasses import replace
from functools import partial

import pytest

from kora.cel import JsonValue
from kora.cel.functions import FUNCTIONS, Form
from kora.cel.limits import MAX_CODE_POINTS
from kora.cel_eval.binding import Binding, NativeProgram
from tests.cel.binding_support import (
    CONTEXT,
    author_evaluated,
    checked,
    evaluated,
    is_value,
    outcome,
    typed,
    unordered,
)
from tests.cel.generator import generated_expressions
from tests.cel.support import accepted
from tests.cel.test_cel_patterns import INVALID, SUPPORTED
from tests.cel.test_cel_rendering import CORPUS, NESTED_AT_LIMIT, conformance_corpus

STAMP: str = "timestamp('2026-01-02T03:04:05.006Z')"
TABLE_CALLS: dict[tuple[str, int, Form], tuple[str, object]] = {
    ("size", 1, Form.GLOBAL): ("size([1, 2])", 2),
    ("size", 0, Form.RECEIVER): ("[1, 2].size()", 2),
    ("int", 1, Form.GLOBAL): ("int('7')", 7),
    ("uint", 1, Form.GLOBAL): ("uint(7)", 7),
    ("double", 1, Form.GLOBAL): ("double(7)", 7.0),
    ("string", 1, Form.GLOBAL): ("string(7)", "7"),
    ("bytes", 1, Form.GLOBAL): ("bytes('a')", b"a"),
    ("type", 1, Form.GLOBAL): ("type(1) == int", True),
    ("dyn", 1, Form.GLOBAL): ("dyn(1)", 1),
    ("duration", 1, Form.GLOBAL): ("duration('90s') == duration('1m30s')", True),
    ("timestamp", 1, Form.GLOBAL): (
        "timestamp('2026-01-02T03:04:05Z') < timestamp('2026-01-02T03:04:06Z')",
        True,
    ),
    ("contains", 1, Form.RECEIVER): ("'abc'.contains('b')", True),
    ("endsWith", 1, Form.RECEIVER): ("'abc'.endsWith('c')", True),
    ("startsWith", 1, Form.RECEIVER): ("'abc'.startsWith('a')", True),
    ("matches", 1, Form.RECEIVER): ("'abc'.matches('^a.c$')", True),
    ("getDate", 0, Form.RECEIVER): (f"{STAMP}.getDate()", 2),
    ("getDayOfMonth", 0, Form.RECEIVER): (f"{STAMP}.getDayOfMonth()", 1),
    ("getDayOfWeek", 0, Form.RECEIVER): (f"{STAMP}.getDayOfWeek()", 5),
    ("getDayOfYear", 0, Form.RECEIVER): (f"{STAMP}.getDayOfYear()", 1),
    ("getFullYear", 0, Form.RECEIVER): (f"{STAMP}.getFullYear()", 2026),
    ("getHours", 0, Form.RECEIVER): (f"{STAMP}.getHours()", 3),
    ("getMilliseconds", 0, Form.RECEIVER): (f"{STAMP}.getMilliseconds()", 6),
    ("getMinutes", 0, Form.RECEIVER): (f"{STAMP}.getMinutes()", 4),
    ("getMonth", 0, Form.RECEIVER): (f"{STAMP}.getMonth()", 0),
    ("getSeconds", 0, Form.RECEIVER): (f"{STAMP}.getSeconds()", 5),
}
REPRESENTATIONS: tuple[str, ...] = (
    "x // note\n  + 1",
    "!!x",
    "--1 == x",
    "'''a\nb''' == x",
    "r'\\d+' == x",
    "0x1F == x",
)
GENERATED_SEED: int = 20261021
GENERATED_COUNT: int = 2000
AT_THE_LIMITS: dict[str, tuple[str, str]] = {
    "length": ("'" + "a" * (MAX_CODE_POINTS - 2) + "'", "str"),
    "four_macros": (
        "[1].all(x, true) && [1].exists(x, true) && "
        "[1].map(x, x).size() == 1 && [1].filter(x, true).size() == 1",
        "bool",
    ),
    "nested_macros": ("[[1]].all(x, x.all(y, y == 1))", "bool"),
    "widest_rendering": ("b'" + "\U0001f600" * (MAX_CODE_POINTS - 3) + "'", "bytes"),
    "escape_density": (
        "'" + "\\U0001f600" * ((MAX_CODE_POINTS - 2) // 10) + "'",
        "str",
    ),
}
PANIC: str = "PanicException"
UNARY_RUNS: tuple[str, ...] = (
    "!!x",
    "!!!x",
    "--x",
    "---x",
    "!!-1",
    "!!!-1",
    "--1",
    "---1",
    "--1.5",
    "- - -1",
    "--1u",
    "---1.f",
    "-(-9223372036854775808)",
    "!!-9223372036854775808",
)
UNARY_OPERANDS: tuple[JsonValue, ...] = (7, True, "s")


def empty_matching(pattern: str) -> str:
    return f"''.matches('{pattern}')"


@pytest.mark.parametrize("entry", sorted(FUNCTIONS, key=repr), ids=repr)
def test_cel_function_table_executes_in_binding(
    cel_binding: Binding, entry: tuple[str, int, Form]
) -> None:
    source, expected = TABLE_CALLS[entry]

    result = evaluated(cel_binding, source)

    assert set(TABLE_CALLS) == set(FUNCTIONS)
    assert result == typed(expected)


def test_cel_regex_subset_agrees_with_binding(cel_binding: Binding) -> None:
    supported = [empty_matching(pattern) for pattern in SUPPORTED]
    invalid = [empty_matching(pattern) for pattern in INVALID]

    accepted = [evaluated(cel_binding, source) for source in supported]
    refused = [author_evaluated(cel_binding, source) for source in invalid]

    assert [result[0] for result in accepted] == ["bool"] * len(supported)
    assert refused == [("error", "RuntimeError")] * len(invalid)


def test_cel_parser_agrees_with_binding_compile(cel_binding: Binding) -> None:
    corpus = [*CORPUS, *generated_expressions(GENERATED_SEED, GENERATED_COUNT)]
    renderings = [accepted(source) for source in corpus]

    compiled = [
        outcome(cel_binding, partial(cel_binding.native_compile, rendering))
        for rendering in renderings
    ]

    assert all(is_value(result) for result in compiled)
    assert len(compiled) > GENERATED_COUNT


def test_cel_binding_compiles_canonical_rendering(cel_binding: Binding) -> None:
    received: list[str] = []

    def recording(expression: str) -> NativeProgram:
        received.append(expression)
        return cel_binding.native_compile(expression)

    recorder = replace(cel_binding, native_compile=recording)
    expressions = [checked(source) for source in REPRESENTATIONS]

    programs = [recorder.compile(expression) for expression in expressions]

    assert received == [expression.rendering for expression in expressions]
    assert all(
        rendering != source
        for rendering, source in zip(received, REPRESENTATIONS, strict=True)
    )
    assert [program.rendering for program in programs] == received


def test_cel_canonical_rendering_matches_author_text_in_binding(
    cel_binding: Binding,
) -> None:
    corpus = [
        *CORPUS,
        *conformance_corpus(),
        *generated_expressions(20261020, GENERATED_COUNT),
    ]

    pairs = [
        (
            author_evaluated(cel_binding, source, CONTEXT, unordered),
            evaluated(cel_binding, source, CONTEXT, unordered),
        )
        for source in corpus
    ]

    values = [rendered for _, rendered in pairs if is_value(rendered)]
    assert [author for author, _ in pairs] == [rendered for _, rendered in pairs]
    assert len(values) >= 1300


@pytest.mark.parametrize("operand", UNARY_OPERANDS, ids=repr)
def test_cel_unary_runs_reduce_as_the_binding_does(
    cel_binding: Binding, operand: JsonValue
) -> None:
    context: dict[str, JsonValue] = {"x": operand}

    pairs = [
        (
            author_evaluated(cel_binding, source, context),
            evaluated(cel_binding, source, context),
        )
        for source in UNARY_RUNS
    ]

    assert [author for author, _ in pairs] == [rendered for _, rendered in pairs]


@pytest.mark.parametrize("shape", sorted(NESTED_AT_LIMIT))
def test_cel_limits_at_boundary_compile_in_binding(
    cel_binding: Binding, shape: str
) -> None:
    rendering = accepted(NESTED_AT_LIMIT[shape]())

    compiled = outcome(cel_binding, partial(cel_binding.native_compile, rendering))
    result = evaluated(cel_binding, NESTED_AT_LIMIT[shape](), CONTEXT)

    assert is_value(compiled)
    assert result[1] != PANIC


@pytest.mark.parametrize("case", sorted(AT_THE_LIMITS))
def test_cel_limits_at_boundary_evaluate_in_binding(
    cel_binding: Binding, case: str
) -> None:
    source, kind = AT_THE_LIMITS[case]

    result = evaluated(cel_binding, source)

    assert len(source) <= MAX_CODE_POINTS
    assert result[0] == kind
