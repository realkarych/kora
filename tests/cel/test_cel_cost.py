from dataclasses import replace

from kora.cel import (
    Budget,
    CheckedExpression,
    Code,
    Diagnostic,
    Estimate,
    Reason,
    ValueBound,
    Variable,
    Weights,
    check,
)
from kora.cel.cost import estimate
from kora.cel.parser import parse
from kora.cel.render import canonical_rendering
from kora.cel.types import DYN, INT, STRING, ListOf
from tests.cel.generator import generated_expressions
from tests.cel.support import accepted

ZERO_WEIGHTS = Weights(
    evaluation=0,
    convert_byte=0,
    convert_value=0,
    node=0,
    iteration=0,
    map_quadratic=0,
    concat_byte=0,
    concat_value=0,
    compare_byte=0,
    compare_value=0,
    function_byte=0,
    literal_value=0,
    pattern_compile=0,
    pattern_match=0,
    rendering_character=0,
    memory_byte=0,
)
UNBOUNDED = Budget(steps=10**30, memory=10**30)
KIB = 1024


def only(**charges: int) -> Weights:
    return replace(ZERO_WEIGHTS, **charges)


def string_bound(size: int) -> ValueBound:
    return ValueBound(bytes=size + 2, values=1, items=0, string_bytes=size)


def string_list(items: int, element: int, total: int) -> Variable:
    bound = ValueBound(
        bytes=total,
        values=items + 1,
        items=items,
        string_bytes=0,
        element=string_bound(element),
    )
    return Variable(ListOf(STRING), bound)


def int_list(items: int) -> Variable:
    element = ValueBound(bytes=20, values=1, items=0, string_bytes=0)
    bound = ValueBound(
        bytes=21 * items + 2,
        values=items + 1,
        items=items,
        string_bytes=0,
        element=element,
    )
    return Variable(ListOf(INT), bound)


def estimated(source: str, weights: Weights, **environment: Variable) -> Estimate:
    tree = parse(source)
    rendering = canonical_rendering(tree)
    return estimate(tree, len(rendering), environment, weights, UNBOUNDED)


def test_cel_cost_estimate_rejects_compounding_concatenation() -> None:
    environment = {"l": string_list(items=1, element=16 * KIB, total=16 * KIB + 4)}
    eight = "+".join(["{v}"] * 8)
    compounding = ".".join(
        ["l"]
        + [f"map({name}, {eight.format(v=name)})" for name in ("a", "b", "c", "d")]
    )
    two = "+".join(["{v}"] * 2)
    modest = ".".join(
        ["l"] + [f"map({name}, {two.format(v=name)})" for name in ("a", "b", "c", "d")]
    )

    accepted(compounding)
    diagnostic = check(compounding, environment)
    fitting = check(modest, environment)

    assert isinstance(diagnostic, Diagnostic)
    assert diagnostic.code is Code.LIMIT
    assert diagnostic.reason is Reason.MEMORY
    assert isinstance(fitting, CheckedExpression)


def test_cel_cost_estimate_bounds_comprehension_results() -> None:
    source = string_list(items=500, element=16 * KIB, total=64 * KIB)
    memory = only(memory_byte=1)

    identity = estimated("l.map(x, x)", memory, l=source)
    doubled = estimated("l.map(x, x + x)", memory, l=source)
    chained = estimated("l.map(x, [x, x]).map(y, y)", memory, l=source)
    filtered = estimated("l.filter(x, true)", memory, l=source)
    naive_bound = 500 * 16 * KIB

    assert identity.memory < 4 * 64 * KIB
    assert doubled.memory < 8 * 64 * KIB
    assert chained.memory < 12 * 64 * KIB
    assert filtered.memory <= 3 * 64 * KIB
    assert max(identity.memory, doubled.memory, chained.memory) < naive_bound // 8


def test_cel_cost_estimate_rejects_quadratic_macros_over_unbounded_lists() -> None:
    over_dyn = check("payload.items.map(x, x)", {"payload": Variable(DYN)})
    over_small = check("l.map(x, x * 2)", {"l": int_list(10)})

    assert isinstance(over_dyn, Diagnostic)
    assert over_dyn.reason is Reason.STEPS
    assert isinstance(over_small, CheckedExpression)


def test_cel_cost_estimate_charges_follow_the_table() -> None:
    strings = {
        "s": Variable(STRING, string_bound(100)),
        "t": Variable(STRING, string_bound(50)),
    }
    numbers = int_list(30)

    compare = estimated("s == t", only(compare_byte=1), **strings)
    nodes = estimated("1 + 2 * 3", only(node=1))
    quadratic = estimated("l.map(x, x)", only(map_quadratic=1), l=numbers)
    iterations = estimated("l.all(x, true)", only(iteration=1), l=numbers)
    body = estimated("l.all(x, x == 1)", only(node=1), l=numbers)
    nested = estimated("l.all(x, l.all(y, true))", only(node=1), l=numbers)
    concat = estimated("s + t", only(concat_byte=1), **strings)
    converted = estimated("s == 'a'", only(convert_byte=1), **strings)
    base = estimated("true", only(evaluation=7))
    compiled = estimated("s.matches('a')", only(pattern_compile=1), **strings)
    matched = estimated("s.matches('a')", only(pattern_match=1), **strings)
    function = estimated("s.contains(t)", only(function_byte=1), **strings)
    rendering = estimated("s.contains(t)", only(rendering_character=3), **strings)
    scoped = estimated("l.all(x, x == y)", ZERO_WEIGHTS, l=numbers)
    held = estimated("s == 'a'", only(memory_byte=1), **strings)

    assert compare.steps == 102 + 52
    assert nodes.steps == 5
    assert quadratic.steps == 30 * 30
    assert iterations.steps == 30
    assert body.steps == 2 + 30 * 3
    assert nested.steps == 2 + 30 * (2 + 30)
    assert concat.steps == 102 + 52
    assert converted.steps == 102
    assert base.steps == 7
    assert compiled.steps == 32
    assert matched.steps == 32 * 102
    assert function.steps == 102 + 52
    assert rendering.compile_steps == 3 * len("s.contains(t)")
    assert scoped.variables == ("l", "y")
    assert held.memory == 102 + 3


def test_cel_cost_estimate_closes_every_comprehension_scope() -> None:
    numbers = {"l": int_list(10)}
    escaping = [
        "l.map(x, [x]).map(y, y[0] + y[0])",
        "l.map(x, {'k': x}).map(y, y.k + y.k)",
        "l.map(x, [x, [x]]).map(y, y[1][0] + 1)",
        "[l.map(x, [x])][0].map(y, y[0] * y[0])",
        "l.map(x, l.map(y, [x, y])).all(z, z[0][1] == 1)",
    ]
    generated = generated_expressions(7, 1000)

    results = [check(source, numbers) for source in escaping]
    outcomes = [check(source) for source in generated]

    assert all(isinstance(result, CheckedExpression) for result in results)
    assert all(
        isinstance(outcome, CheckedExpression | Diagnostic) for outcome in outcomes
    )
