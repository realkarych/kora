from types import MappingProxyType
from typing import cast

import pytest

from kora.cel import (
    BoundError,
    CheckedExpression,
    Diagnostic,
    Environment,
    JsonValue,
    Reason,
    ResultKind,
    SchemaTypes,
    SchemaView,
    Variable,
    check,
    check_typed,
)
from kora.cel.parser import RENDERING_LIMITS, parse
from kora.cel.types import DYN, CelType, Record, RecordField

FORM: JsonValue = {
    "type": "object",
    "properties": {
        "decision": {"enum": ["approve", "reject"]},
        "comments": {"type": "string"},
        "note": {"type": ["string", "null"]},
    },
    "required": ["decision", "note"],
}
UNTYPED_FORM: JsonValue = {
    "properties": {"decision": {"enum": ["approve", "reject"]}},
    "required": ["decision"],
}
LABEL: JsonValue = {"type": "string"}
METRICS: JsonValue = {
    "type": "object",
    "properties": {
        "cpu": {"type": "number"},
        "cpu_seconds": {"type": "number"},
        "exit_code": {"type": "integer"},
        "count": {"type": "integer"},
        "name": {"type": "string"},
        "tags": {"type": "array", "items": LABEL},
        "labels": {"type": "object", "additionalProperties": LABEL},
        "facts": {
            "type": "object",
            "properties": {"change": {"type": "string"}},
            "required": ["change"],
        },
        "closed": {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        },
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        },
        "maybe": {"type": ["integer", "null"]},
        "optional": {"type": "integer"},
        "meta": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
        },
    },
    "required": [
        "cpu",
        "cpu_seconds",
        "exit_code",
        "count",
        "name",
        "tags",
        "labels",
        "facts",
        "closed",
        "notes",
        "maybe",
    ],
}
INTEGER_CPU: JsonValue = {
    "type": "object",
    "properties": {"cpu": {"type": "integer"}},
    "required": ["cpu"],
}
VERDICT: JsonValue = {
    "type": "object",
    "properties": {"verdict": {"enum": ["approved", "rejected"]}},
    "required": ["verdict"],
}


def no_documents(uri: str) -> JsonValue | None:
    return None


def typed(schema: JsonValue) -> CelType:
    return SchemaTypes(no_documents).document(schema, SchemaView.STRUCTURED_OUTPUT)


def environment(result: ResultKind = ResultKind.BOOL) -> Environment:
    metrics = typed(METRICS)
    inputs = Record(
        {
            "bench": RecordField(metrics, True),
            "baseline": RecordField(metrics, True),
            "same": RecordField(metrics, True),
            "metrics": RecordField(metrics, True),
            "bundle": RecordField(metrics, True),
            "review": RecordField(typed(VERDICT), True),
            "previous": RecordField(metrics, False),
        }
    )
    outputs = Record({"result": RecordField(metrics, False)})
    return Environment(
        {
            "inputs": Variable(inputs),
            "outputs": Variable(outputs),
            "params": Variable(typed({"type": "object", "properties": {}})),
            "response": Variable(typed(FORM)),
            "untyped": Variable(typed(UNTYPED_FORM)),
            "payload": Variable(DYN),
            "observation": Variable(DYN),
        },
        result,
        frozenset({"inputs", "outputs"}),
    )


def rejection(source: str, result: ResultKind = ResultKind.BOOL) -> Diagnostic:
    outcome = check_typed(source, environment(result))
    assert isinstance(outcome, Diagnostic), f"accepted: {source}"
    return outcome


Outcome = tuple[Reason | None, int | None]
ACCEPTED: Outcome = (None, None)


def outcome(source: str, result: ResultKind = ResultKind.BOOL) -> Outcome:
    checked = check_typed(source, environment(result))
    if isinstance(checked, Diagnostic):
        return checked.reason, checked.offset
    return ACCEPTED


@pytest.mark.parametrize(
    ("source", "offset"),
    [
        ("response.decison == 'approve'", 9),
        ("inputs.bundle.facts.extra == 'a'", 20),
        ("inputs.bundle.labels.k == 'a'", 21),
        ("inputs.bundle.closed['b'] == 1", 20),
        ("has(inputs.bundle.labels.k)", 25),
        ("has(response.missing)", 13),
    ],
)
def test_cel_rejects_unknown_field(source: str, offset: int) -> None:
    result = rejection(source)

    assert (result.reason, result.code.value, result.offset) == (
        Reason.UNKNOWN_FIELD,
        "IR011",
        offset,
    )


@pytest.mark.parametrize(
    ("source", "result_kind"),
    [
        ("response.decision", ResultKind.BOOL),
        ("inputs.bundle.cpu", ResultKind.BOOL),
        ("response.decision == 'approve'", ResultKind.STRING),
        ("[true][0] ? response.note : 'x'", ResultKind.STRING),
    ],
)
def test_cel_rejects_non_boolean_approve_when(
    source: str, result_kind: ResultKind
) -> None:
    result = rejection(source, result_kind)

    assert (result.reason, result.offset) == (Reason.WRONG_RESULT_TYPE, 0)


def test_cel_converts_json_numbers_to_schema_types() -> None:
    example = (
        "inputs.review.verdict == 'approved' && "
        "inputs.bench.cpu < inputs.baseline.cpu * 0.85"
    )
    integer_inputs = Record(
        {
            "review": RecordField(typed(VERDICT), True),
            "bench": RecordField(typed(INTEGER_CPU), True),
            "baseline": RecordField(typed(INTEGER_CPU), True),
        }
    )
    integers = Environment({"inputs": Variable(integer_inputs)}, ResultKind.BOOL)
    converted = example.replace(
        "inputs.baseline.cpu * 0.85", "double(inputs.baseline.cpu) * 0.85"
    )

    numbers = check_typed(example, environment())
    refused = check_typed(example, integers)
    explicit = check_typed(converted, integers)

    assert isinstance(numbers, CheckedExpression)
    assert isinstance(refused, Diagnostic)
    assert (refused.reason, refused.offset) == (
        Reason.NO_MATCHING_OVERLOAD,
        example.index("*"),
    )
    assert isinstance(explicit, CheckedExpression)


ENUM_LITERALS: list[tuple[str, Outcome]] = [
    ("response.decision == 'aprove'", (Reason.UNKNOWN_ENUM_VALUE, 21)),
    ("'aprove' == response.decision", (Reason.UNKNOWN_ENUM_VALUE, 0)),
    ("response.decision != 'aprove'", (Reason.UNKNOWN_ENUM_VALUE, 21)),
    ("untyped.decision == 'aprove'", (Reason.UNKNOWN_ENUM_VALUE, 20)),
    ("response.decision in ['approve', 'aprove']", (Reason.UNKNOWN_ENUM_VALUE, 33)),
    ("inputs.review.verdict == 'approve'", (Reason.UNKNOWN_ENUM_VALUE, 25)),
    ("response.decision == 'approve'", ACCEPTED),
    ("untyped.decision != 'reject'", ACCEPTED),
    ("response.decision in ['approve', 'reject']", ACCEPTED),
    ("response.decision == inputs.bundle.name", ACCEPTED),
]


@pytest.mark.parametrize(("source", "expected"), ENUM_LITERALS)
def test_cel_rejects_unknown_enum_literal(source: str, expected: Outcome) -> None:
    result = outcome(source)

    assert result == expected


MIXED_NUMBERS: list[tuple[str, Outcome]] = [
    ("inputs.same.exit_code == 0.0", (Reason.NO_MATCHING_OVERLOAD, 22)),
    ("0.0 != inputs.same.exit_code", (Reason.NO_MATCHING_OVERLOAD, 4)),
    ("inputs.same.cpu == 1", (Reason.NO_MATCHING_OVERLOAD, 16)),
    ("1u == 1", (Reason.NO_MATCHING_OVERLOAD, 3)),
    ("inputs.same.exit_code in [0.0, 1.0]", (Reason.NO_MATCHING_OVERLOAD, 22)),
    ("inputs.metrics.cpu_seconds < 100", ACCEPTED),
    ("inputs.same.exit_code == 0", ACCEPTED),
    ("inputs.same.exit_code <= 1.5", ACCEPTED),
    ("1u < 2.0", ACCEPTED),
]


@pytest.mark.parametrize(("source", "expected"), MIXED_NUMBERS)
def test_cel_rejects_mixed_int_double_equality(source: str, expected: Outcome) -> None:
    result = outcome(source)

    assert result == expected


def unguarded(offset: int) -> Outcome:
    return Reason.UNGUARDED_OPTIONAL, offset


OPTIONAL_READS: list[tuple[str, Outcome]] = [
    ("response.comments == 'x'", unguarded(9)),
    ("outputs.result.exit_code == 0", unguarded(8)),
    ("inputs.previous.count > 1", unguarded(7)),
    ("has(inputs.previous.meta)", unguarded(11)),
    ("inputs.bundle.optional > 1 && has(inputs.bundle.optional)", unguarded(14)),
    ("has(response.comments) || response.comments == 'x'", unguarded(35)),
    ("!has(response.comments) && response.comments == 'x'", unguarded(36)),
    ("has(response.comments) ? false : response.comments == 'x'", unguarded(42)),
    ("inputs.bundle.labels['k'] == 'a'", unguarded(20)),
    ("'k' in inputs.bundle.tags && inputs.bundle.labels['k'] == 'a'", unguarded(49)),
    ("has(inputs.bundle.meta) && inputs.bundle.meta.n == 1", unguarded(46)),
    ("inputs.bundle.notes.all(n, n.text == 'x')", unguarded(29)),
    ("[inputs.bundle.notes[0]][0].text == 'x'", unguarded(28)),
    ("has(response.comments) && response.comments == 'x'", ACCEPTED),
    ("true && has(response.comments) && 1 < 2 && response.comments == 'x'", ACCEPTED),
    ("(has(response.comments) && true) && response.comments == 'x'", ACCEPTED),
    ("!has(response.comments) || response.comments == 'x'", ACCEPTED),
    ("false || !has(response.comments) || response.comments == 'x'", ACCEPTED),
    ("!(!has(response.comments) || false) && response.comments == 'x'", ACCEPTED),
    ("has(response.comments) ? response.comments == 'x' : false", ACCEPTED),
    ("has(outputs.result) && outputs.result.exit_code == 0", ACCEPTED),
    (
        "has(inputs.previous) && has(inputs.previous.meta) && "
        "has(inputs.previous.meta.n) && inputs.previous.meta.n > 1",
        ACCEPTED,
    ),
    ("'k' in inputs.bundle.labels && inputs.bundle.labels['k'] == 'a'", ACCEPTED),
    ("'optional' in inputs.bundle && inputs.bundle.optional > 0", ACCEPTED),
    ("has(inputs.bundle.optional) && inputs.bundle['optional'] > 0", ACCEPTED),
    ("inputs.bundle.labels[response.decision] == 'a'", ACCEPTED),
    ("inputs.bundle.notes.all(n, has(n.text) && n.text == 'x')", ACCEPTED),
    (
        "has(inputs.bundle.notes[0].text) && inputs.bundle.notes[0].text == 'x'",
        ACCEPTED,
    ),
    ("has(response.comments) && [1].all(x, response.comments == 'x')", ACCEPTED),
]


@pytest.mark.parametrize(("source", "expected"), OPTIONAL_READS)
def test_cel_rejects_unguarded_optional_field(source: str, expected: Outcome) -> None:
    result = outcome(source)

    assert result == expected


@pytest.mark.parametrize(
    "source",
    [
        "observation.body.ready == true",
        "payload.commit == inputs.bundle.facts.change",
        "payload['key'].nested == 1",
        "payload.items.exists(i, i.ok)",
    ],
)
def test_cel_dyn_selection_needs_no_guard(source: str) -> None:
    result = outcome(source)

    assert result == ACCEPTED


@pytest.mark.parametrize(
    ("source", "result_kind"),
    [
        ("observation.body.ready", ResultKind.BOOL),
        ("payload.reason", ResultKind.STRING),
    ],
)
def test_cel_dyn_result_requires_exact_bool(
    source: str, result_kind: ResultKind
) -> None:
    result = outcome(source, result_kind)

    assert result == ACCEPTED


STRING_SIZES: list[tuple[str, Outcome]] = [
    ("size(inputs.bundle.name) > 0", (Reason.STRING_SIZE, 0)),
    ("inputs.bundle.name.size() > 0", (Reason.STRING_SIZE, 19)),
    ("size(response.decision) > 0", (Reason.STRING_SIZE, 0)),
    ("size(payload) > 0", (Reason.STRING_SIZE, 0)),
    ("payload.text.size() > 0", (Reason.STRING_SIZE, 13)),
    ("size(inputs.bundle.tags) > 0", ACCEPTED),
    ("inputs.bundle.labels.size() > 0", ACCEPTED),
    ("size(inputs.bundle.facts) == 1", ACCEPTED),
    ("size(b'ab') == 2", ACCEPTED),
    ("size([1, 2]) == 2", ACCEPTED),
]


@pytest.mark.parametrize(("source", "expected"), STRING_SIZES)
def test_cel_rejects_string_size(source: str, expected: Outcome) -> None:
    result = outcome(source)

    assert result == expected


@pytest.mark.parametrize(
    ("source", "reason", "offset"),
    [
        ("unknown == 1", Reason.UNKNOWN_IDENTIFIER, 0),
        ("[1].all(x, y > 0)", Reason.UNKNOWN_IDENTIFIER, 11),
        ("inputs == null", Reason.ROOT_NOT_SELECTED, 0),
        ("inputs['bundle'].count > 0", Reason.ROOT_NOT_SELECTED, 0),
        ("size(outputs) > 0", Reason.ROOT_NOT_SELECTED, 5),
        ("inputs.exists(k, k == 'a')", Reason.ROOT_NOT_SELECTED, 0),
    ],
)
def test_cel_rejects_unknown_identifiers_and_whole_roots(
    source: str, reason: Reason, offset: int
) -> None:
    result = rejection(source)

    assert (result.reason, result.offset) == (reason, offset)


def test_cel_rejects_shadowing_comprehension_variable() -> None:
    shadowing = rejection("inputs.bundle.tags.all(params, true)")
    nested = check_typed("[[1]].all(x, x.all(x, x > 0))", environment())
    distinct = check_typed("[1].all(x, [2].exists(y, x < y))", environment())

    assert (shadowing.reason, shadowing.offset) == (Reason.SHADOWED_VARIABLE, 19)
    assert isinstance(nested, CheckedExpression)
    assert isinstance(distinct, CheckedExpression)


OVERLOADS: list[str] = [
    "'a' + 1 == 'a1'",
    "!1",
    "-true == false",
    "-'a' == 'a'",
    "-1u == 1u",
    "1 && true",
    "true || 'a'",
    "(1 ? true : false)",
    "1 < 'a'",
    "[1] < [2]",
    "inputs.bundle.facts < inputs.bundle.facts",
    "1 in 'abc'",
    "'a' in [1]",
    "1 in inputs.bundle.facts",
    "1.0 % 2.0 == 1.0",
    "inputs.bundle.cpu + 1 > 2.0",
    "1u + 1 > 2u",
    "inputs.bundle.tags + 'a' == []",
    "inputs.bundle.facts + inputs.bundle.facts == {}",
    "true + true",
    "size(1) == 1",
    "int(true) == 1",
    "bytes(1) == b''",
    "'a'.contains(1)",
    "inputs.bundle.count.contains('a')",
    "inputs.bundle.tags[1u] == 'a'",
    "inputs.bundle.tags['a'] == 'a'",
    "inputs.bundle.labels[1] == 'a'",
    "inputs.bundle.facts[1] == 'a'",
    "inputs.bundle.name.x == 1",
    "inputs.bundle.name[0] == 'a'",
    "has(inputs.bundle.name.x)",
    "{1.5: 'a'}.size() == 1",
    "{null: 1}.size() == 1",
    "{inputs.bundle.maybe: 1}.size() == 1",
    "{1: 'a', 2.5: 'b'}.size() == 2",
    "{1: 'a', [1]: 'b'}.size() == 2",
    "{'a': 1, {'x': 1}: 2}.size() == 2",
    "{} in {}",
    "[1] in {'a': 1}",
    "{}[1.5] == 1",
    "inputs.bundle.labels[1.5] == 'a'",
    "inputs.bundle.count == null",
    "inputs.bundle.facts == null",
    "[1, 2].all(x, x)",
    "[1].map(x, x, x) == [1]",
    "[1].filter(x, 'a') == [1]",
    "1.all(x, true)",
    "timestamp('2020-01-01T00:00:00Z') + timestamp('2020-01-01T00:00:00Z') == null",
    "duration('1s') - timestamp('2020-01-01T00:00:00Z') == duration('1s')",
]


@pytest.mark.parametrize("source", OVERLOADS)
def test_cel_rejects_operand_overloads(source: str) -> None:
    result = rejection(source)

    assert (result.reason, result.code.value) == (Reason.NO_MATCHING_OVERLOAD, "IR011")


TIME: str = "timestamp('2026-10-27T00:00:00Z')"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (f"int({TIME}) > 0", Reason.NO_MATCHING_OVERLOAD),
        (f"timestamp(1) < {TIME}", Reason.NO_MATCHING_OVERLOAD),
        (f"duration('1s') + {TIME} > {TIME}", Reason.NO_MATCHING_OVERLOAD),
        ("duration('1s').getFullYear() > 0", Reason.NO_MATCHING_OVERLOAD),
        ("duration('1s').getDayOfWeek() > 0", Reason.NO_MATCHING_OVERLOAD),
        ("duration('1s').getMilliseconds() > 0", Reason.NO_MATCHING_OVERLOAD),
        ("{'a': 1, 'a': 2}.size() == 1", Reason.REPEATED_MAP_KEY),
        ("{1: 'a', 2: 'b', 1: 'c'}.size() == 2", Reason.REPEATED_MAP_KEY),
        ("string(true) == 'true'", Reason.NO_MATCHING_OVERLOAD),
        ("string(payload.flag == true) == 'true'", Reason.NO_MATCHING_OVERLOAD),
        ("-payload.flag == true", Reason.NO_MATCHING_OVERLOAD),
        ("-dyn(1) == -1", Reason.NO_MATCHING_OVERLOAD),
        ("payload.elapsed.getMilliseconds() > 0", Reason.NO_MATCHING_OVERLOAD),
    ],
)
def test_cel_rejects_overloads_the_binding_lacks(source: str, reason: Reason) -> None:
    result = rejection(source)

    assert result.reason is reason


WELL_TYPED: list[tuple[str, ResultKind]] = [
    (f"{TIME} + duration('1s') > {TIME}", ResultKind.BOOL),
    (f"{TIME} - {TIME} == duration('0s')", ResultKind.BOOL),
    (
        f"{TIME}.getMilliseconds() >= 0 && duration('1h').getHours() == 1",
        ResultKind.BOOL,
    ),
    (f"string({TIME}) != '' && int('1') == 1 && uint(1) == 1u", ResultKind.BOOL),
    ("double(inputs.bundle.count) * 0.85 < inputs.bundle.cpu", ResultKind.BOOL),
    ("inputs.bundle.maybe == null || inputs.bundle.maybe > 1", ResultKind.BOOL),
    (
        "{1: 'a'}[inputs.bundle.maybe] == 'a' && inputs.bundle.maybe in {1: 'a'}",
        ResultKind.BOOL,
    ),
    ("(true ? 1 : null) == null", ResultKind.BOOL),
    ("[1, 'a'].size() == 2 && {'a': 1, 'b': 'c'}.size() == 2", ResultKind.BOOL),
    ("inputs.bundle.tags + ['x'] == ['x']", ResultKind.BOOL),
    ("inputs.bundle.tags.map(t, t + '!').exists_one(t, t == 'a!')", ResultKind.BOOL),
    ("inputs.bundle.tags.filter(t, t != '').all(t, t.endsWith('x'))", ResultKind.BOOL),
    ("inputs.bundle.labels.exists(k, inputs.bundle.labels[k] == 'v')", ResultKind.BOOL),
    ("inputs.bundle.facts.all(k, k.startsWith('c'))", ResultKind.BOOL),
    (
        "inputs.bundle.name.matches('^a+$') && 'b' in inputs.bundle.labels",
        ResultKind.BOOL,
    ),
    ("type(payload) == map && dyn(1) == 1", ResultKind.BOOL),
    ("payload.n + 1 > 2 && -double(payload.m) < 0.0 && !payload.flag", ResultKind.BOOL),
    ("bytes('a') + b'b' == b'ab' && 7 % 2 == 1 && 5u % 2u == 1u", ResultKind.BOOL),
    ("response.decision", ResultKind.STRING),
    ("response.decision == 'approve' ? 'ok' : inputs.bundle.name", ResultKind.STRING),
    ("payload.reason", ResultKind.STRING),
]


@pytest.mark.parametrize(("source", "result_kind"), WELL_TYPED)
def test_cel_accepts_well_typed_expressions(
    source: str, result_kind: ResultKind
) -> None:
    result = check_typed(source, environment(result_kind))

    assert isinstance(result, CheckedExpression), result


def test_cel_typed_check_refuses_malformed_environments() -> None:
    proxied = Environment(
        cast("dict[str, Variable]", MappingProxyType({"v": Variable(DYN)})),
        ResultKind.BOOL,
    )
    listed = Environment(cast("dict[str, Variable]", [Variable(DYN)]), ResultKind.BOOL)

    with pytest.raises(BoundError, match="not a plain dict"):
        check_typed("v == 1", proxied)
    with pytest.raises(BoundError, match="not a plain dict"):
        check_typed("v == 1", listed)


INTEGER_NEGATIONS: list[tuple[str, str]] = [
    ("-inputs.bundle.count < 0", "((0 - inputs.bundle.count) < 0)"),
    ("-(-inputs.bundle.count) == 1", "((0 - (0 - inputs.bundle.count)) == 1)"),
    ("---inputs.bundle.count == 1", "((0 - inputs.bundle.count) == 1)"),
    ("--inputs.bundle.count == 1", "(inputs.bundle.count == 1)"),
    ("-(5) == -5", "((0 - 5) == -5)"),
    ("-(-9223372036854775808) == 0", "((0 - -9223372036854775808) == 0)"),
    (
        "inputs.bundle.maybe != null && -inputs.bundle.maybe < 0",
        "((inputs.bundle.maybe != null) && ((0 - inputs.bundle.maybe) < 0))",
    ),
    (
        "[inputs.bundle.count].all(n, -n < 0 && {-n: -n}.size() == 1)",
        "[inputs.bundle.count].all(n, (((0 - n) < 0) && "
        "({(0 - n): (0 - n)}.size() == 1)))",
    ),
    (
        "(inputs.bundle.count > 0 ? inputs.bundle.count : -inputs.bundle.count) > 0",
        "(((inputs.bundle.count > 0) ? inputs.bundle.count : "
        "(0 - inputs.bundle.count)) > 0)",
    ),
    ("-inputs.bundle.cpu < -0.0", "((-inputs.bundle.cpu) < -0.0)"),
    ("-double(payload.m) < 0.0", "((-double(payload.m)) < 0.0)"),
]


@pytest.mark.parametrize(("source", "rendering"), INTEGER_NEGATIONS)
def test_cel_typed_rendering_rewrites_integer_negation(
    source: str, rendering: str
) -> None:
    typed_check = check_typed(source, environment())
    untyped_check = check(source)

    assert isinstance(typed_check, CheckedExpression)
    assert isinstance(untyped_check, CheckedExpression)
    assert typed_check.rendering == rendering
    assert parse(typed_check.rendering, RENDERING_LIMITS) == typed_check.tree
    assert untyped_check.tree == parse(source)


def test_cel_typed_estimate_is_of_the_rewritten_tree() -> None:
    negated = check_typed("-inputs.bundle.count < 0", environment())
    subtracted = check_typed("0 - inputs.bundle.count < 0", environment())

    assert isinstance(negated, CheckedExpression)
    assert isinstance(subtracted, CheckedExpression)
    assert negated.tree == subtracted.tree
    assert negated.rendering == subtracted.rendering
    assert negated.estimate == subtracted.estimate
