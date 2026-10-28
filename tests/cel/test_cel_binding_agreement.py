from dataclasses import replace
from functools import partial

import pytest

from kora.cel import (
    CheckedExpression,
    Diagnostic,
    Environment,
    JsonValue,
    Reason,
    ResultKind,
    Variable,
    check_typed,
)
from kora.cel.functions import FUNCTIONS, Form
from kora.cel.limits import MAX_CODE_POINTS
from kora.cel.types import BOOL, INT, STRING, CelType, ListOf, Record, RecordField
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
from tests.cel.support import accepted, checked_syntax
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


def recursive_record() -> Record:
    fields: dict[str, RecordField] = {"x": RecordField(BOOL, True)}
    record = Record(fields, None)
    fields["r"] = RecordField(record, True)
    return record


def nested_record_value(depth: int) -> JsonValue:
    value: JsonValue = {"x": True}
    for _ in range(depth):
        value = {"r": value, "x": True}
    return value


def nested_list_type(depth: int) -> CelType:
    cel_type: CelType = BOOL
    for _ in range(depth):
        cel_type = ListOf(cel_type)
    return cel_type


def nested_list_value(depth: int) -> JsonValue:
    value: JsonValue = True
    for _ in range(depth):
        value = [value]
    return value


Bindings = dict[str, tuple[CelType, JsonValue]]
FLAG: Bindings = {"x": (BOOL, True)}
TEXT: Bindings = {"s": (STRING, "s")}
NUMBER: Bindings = {"n": (INT, 7)}
TYPED_AT_LIMIT: dict[str, tuple[str, Bindings, ResultKind]] = {
    "addition": (" + ".join(["s"] * 33), TEXT, ResultKind.STRING),
    "conjunction": (" && ".join(["x"] * 33), FLAG, ResultKind.BOOL),
    "selection": (
        "r" + ".r" * 31 + ".x",
        {"r": (recursive_record(), nested_record_value(31))},
        ResultKind.BOOL,
    ),
    "index": (
        "l" + "[0]" * 32,
        {"l": (nested_list_type(32), nested_list_value(32))},
        ResultKind.BOOL,
    ),
    "conditional": ("x ? x : " * 32 + "x", FLAG, ResultKind.BOOL),
    "negation": ("!" * 32 + "x", FLAG, ResultKind.BOOL),
    "parenthesized_negation": ("!(" * 16 + "x" + ")" * 16, FLAG, ResultKind.BOOL),
    "minus_on_variable": ("string(" + "-" * 31 + "n)", NUMBER, ResultKind.STRING),
    "minus_on_literal": ("string(" + "-" * 31 + "1)", {}, ResultKind.STRING),
    "parenthesized_minus": (
        "string(" + "-(" * 15 + "-n" + ")" * 15 + ")",
        NUMBER,
        ResultKind.STRING,
    ),
    "call_argument": (
        "string(" + "int(" * 31 + "n" + ")" * 32,
        NUMBER,
        ResultKind.STRING,
    ),
    "list_of_binary": (
        "[" * 16 + "s + " * 15 + "s" + "]" * 16 + " == []",
        TEXT,
        ResultKind.BOOL,
    ),
    "map_values": ("{1: " * 31 + "x" + "}" * 31 + " == {}", FLAG, ResultKind.BOOL),
    "macro_body": (
        "l.all(v, " + " && ".join(["v"] * 32) + ")",
        {"l": (ListOf(BOOL), [True])},
        ResultKind.BOOL,
    ),
}


def typed_environment(bindings: Bindings, result: ResultKind) -> Environment:
    variables = {name: Variable(cel_type) for name, (cel_type, _) in bindings.items()}
    return Environment(variables, result)


@pytest.mark.parametrize(
    ("escape", "count", "expected"), [("00", 8, "\x00" * 8), ("ff", 20, "\ufffd" * 20)]
)
@pytest.mark.parametrize("dynamic", [False, True])
def test_cel_bytes_to_string_preserves_native_expansion(
    cel_binding: Binding, escape: str, count: int, expected: str, dynamic: bool
) -> None:
    literal = "b'" + f"\\x{escape}" * count + "'"
    operand = f"dyn({literal})" if dynamic else literal

    result = evaluated(cel_binding, f"string({operand})")

    assert result == typed(expected)


@pytest.mark.parametrize("pattern", ["", "()" * 20, "|" * 31, "(?i)" * 16, "(?:)" * 16])
def test_cel_empty_pattern_structures_execute(
    cel_binding: Binding, pattern: str
) -> None:
    source = f"''.matches('{pattern}')"

    result = evaluated(cel_binding, source)

    assert result == ("bool", "True")


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
OUT_OF_RANGE_RUNS: tuple[str, ...] = (
    "--9223372036854775808",
    "---9223372036854775808",
    "---0x8000000000000000",
)


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


def test_cel_unary_runs_kora_rejects_are_refused_by_the_binding(
    cel_binding: Binding,
) -> None:
    sources = OUT_OF_RANGE_RUNS

    diagnostics = [checked_syntax(source) for source in sources]
    refused = [author_evaluated(cel_binding, source) for source in sources]

    assert [
        diagnostic.reason if isinstance(diagnostic, Diagnostic) else None
        for diagnostic in diagnostics
    ] == [Reason.INVALID_LITERAL] * len(sources)
    assert refused == [("error", "ValueError")] * len(sources)


@pytest.mark.parametrize("shape", sorted(NESTED_AT_LIMIT))
def test_cel_limits_at_boundary_compile_in_binding(
    cel_binding: Binding, shape: str
) -> None:
    rendering = accepted(NESTED_AT_LIMIT[shape]())

    compiled = outcome(cel_binding, partial(cel_binding.native_compile, rendering))
    result = evaluated(cel_binding, NESTED_AT_LIMIT[shape](), CONTEXT)

    assert is_value(compiled)
    assert result[1] != PANIC


@pytest.mark.parametrize("shape", sorted(TYPED_AT_LIMIT))
def test_cel_typed_limits_at_boundary_compile_in_binding(
    cel_binding: Binding, shape: str
) -> None:
    source, bindings, result_kind = TYPED_AT_LIMIT[shape]
    environment = typed_environment(bindings, result_kind)
    context = {name: value for name, (_, value) in bindings.items()}

    checked = check_typed(source, environment)
    deeper = check_typed(f"({source})", environment)

    assert isinstance(checked, CheckedExpression), checked
    assert isinstance(deeper, Diagnostic)
    assert deeper.reason is Reason.DEPTH
    compiled = outcome(
        cel_binding, partial(cel_binding.native_compile, checked.rendering)
    )
    result = outcome(
        cel_binding,
        lambda: cel_binding.native_compile(checked.rendering).execute(dict(context)),
    )
    assert is_value(compiled)
    assert is_value(result), result


@pytest.mark.parametrize("case", sorted(AT_THE_LIMITS))
def test_cel_limits_at_boundary_evaluate_in_binding(
    cel_binding: Binding, case: str
) -> None:
    source, kind = AT_THE_LIMITS[case]

    result = evaluated(cel_binding, source)

    assert len(source) <= MAX_CODE_POINTS
    assert result[0] == kind
