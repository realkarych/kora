from collections import ChainMap
from dataclasses import replace
from types import MappingProxyType
from typing import cast

import pytest

from kora.cel import (
    BoundError,
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
from kora.cel.types import (
    DOUBLE,
    DYN,
    INT,
    STRING,
    CelType,
    Kind,
    ListOf,
    Nullable,
    Primitive,
    Record,
    RecordField,
)
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
    counted = estimated("s == 'a'", only(convert_value=1), **strings)
    compared_values = estimated("s == t", only(compare_value=1), **strings)
    membership = estimated("s in [t]", only(compare_value=1), **strings)
    joined_values = estimated("s + t", only(concat_value=1), **strings)
    listed = estimated("[s, t]", only(concat_byte=1), **strings)
    listed_values = estimated("[s, t]", only(concat_value=1), **strings)
    mapped = estimated("{'k': s}", only(concat_byte=1), **strings)
    integer = estimated("int(s)", only(function_byte=1), **strings)
    filtered = estimated("l.filter(x, true)", only(map_quadratic=1), l=numbers)
    pattern_memory = estimated("s.matches('a')", only(memory_byte=1), **strings)

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
    assert counted.steps == 1
    assert compared_values.steps == 1 + 1
    assert membership.steps == 1 + (1 + 1)
    assert joined_values.steps == 1 + 1
    assert listed.steps == 102 + 52 + 3
    assert listed_values.steps == 1 + 1 + 1
    assert mapped.steps == 3 + 102 + 3
    assert integer.steps == 102
    assert filtered.steps == 30 * 30
    assert pattern_memory.memory == 102 + 3 + 32


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
    assert any(isinstance(outcome, CheckedExpression) for outcome in outcomes)
    assert any(isinstance(outcome, Diagnostic) for outcome in outcomes)


def test_cel_cost_estimate_keeps_element_bounds_through_literals() -> None:
    nested = ValueBound(bytes=60_000, values=30_001, items=1000, string_bytes=0)
    lists = Variable(
        ListOf(ListOf(INT)),
        ValueBound(
            bytes=60_000, values=30_001, items=30, string_bytes=0, element=nested
        ),
    )
    wrapped = "dyn([" + ", ".join(["l"] * 80) + "]).map(x, x.map(y, 1)).size() > 0"
    sources = [
        wrapped,
        "([l] + [1]).map(x, x.map(y, 1))",
        "(true ? [l] : [1]).map(x, x.map(y, 1))",
    ]

    untyped = [check(source) for source in sources]
    typed = check("([[1]] + l).all(x, x.map(y, 1).size() > 0)", {"l": lists})

    for diagnostic in [*untyped, typed]:
        assert isinstance(diagnostic, Diagnostic)
        assert diagnostic.reason is Reason.STEPS


def test_cel_cost_estimate_survives_deep_shared_and_cyclic_environments() -> None:
    deep = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    for _ in range(3000):
        deep = ValueBound(
            bytes=60_000, values=30_000, items=1, string_bytes=0, element=deep
        )
    first = shared_record(32)
    second = shared_record(32)
    first_fields: dict[str, RecordField] = {}
    cyclic_first = Record(first_fields)
    first_fields["next"] = RecordField(cyclic_first, True)
    second_fields: dict[str, RecordField] = {}
    cyclic_second = Record(second_fields)
    second_fields["next"] = RecordField(cyclic_second, True)
    bound = ValueBound(bytes=60_000, values=30_000, items=2, string_bytes=0)

    chained = chained_shared_bounds(16)
    looped_fields: dict[str, ValueBound] = {}
    looped = ValueBound(
        bytes=60_000, values=30_000, items=2, string_bytes=0, fields=looped_fields
    )
    looped_fields["self"] = looped

    nested = check("v[0][0] == 1", {"v": Variable(DYN, deep)})
    chain = check("[" + ", ".join(sorted(chained)) + "]", chained)
    loop = check("v.self.self == 1", {"v": Variable(DYN, looped)})
    shared = check(
        "[v, w]", {"v": Variable(first, bound), "w": Variable(second, bound)}
    )
    cyclic = check(
        "[v, w, v.next, w.next.next]",
        {"v": Variable(cyclic_first, bound), "w": Variable(cyclic_second, bound)},
    )

    assert isinstance(nested, CheckedExpression)
    assert isinstance(chain, CheckedExpression | Diagnostic)
    assert isinstance(loop, CheckedExpression)
    assert isinstance(shared, CheckedExpression)
    assert isinstance(cyclic, CheckedExpression)


def shared_record(depth: int) -> CelType:
    cel_type: CelType = INT
    for _ in range(depth):
        cel_type = Record(
            {"a": RecordField(cel_type, True), "b": RecordField(cel_type, True)}
        )
    return cel_type


def chained_shared_bounds(count: int) -> dict[str, Variable]:
    previous = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    variables: dict[str, Variable] = {}
    for index in range(count):
        bound = previous
        for _ in range(31):
            bound = ValueBound(
                bytes=60_000, values=30_000, items=1, string_bytes=0, element=bound
            )
        variables[f"v{index}"] = Variable(DYN, bound)
        previous = bound
    return variables


def test_cel_cost_estimate_does_not_depend_on_operand_order() -> None:
    inner = ValueBound(bytes=100, values=10, items=10, string_bytes=0)
    shallow = ValueBound(
        bytes=1000, values=100, items=10, string_bytes=0, element=inner
    )
    deep = shallow
    for _ in range(32):
        deep = ValueBound(
            bytes=60_000, values=30_000, items=1, string_bytes=0, element=deep
        )
    environment = {"a": Variable(DYN, shallow), "b": Variable(DYN, deep)}

    first = check("a[0].map(x, x) == [] && b == b", environment)
    second = check("b == b && a[0].map(x, x) == []", environment)

    assert isinstance(first, CheckedExpression)
    assert isinstance(second, CheckedExpression)
    assert first.estimate.steps == second.estimate.steps


def cyclic_pair(first_bytes: int, second_bytes: int) -> dict[str, Variable]:
    first_fields: dict[str, ValueBound] = {}
    second_fields: dict[str, ValueBound] = {}
    first = ValueBound(
        bytes=first_bytes, values=100, items=5, string_bytes=0, fields=first_fields
    )
    second = ValueBound(
        bytes=second_bytes, values=100, items=3, string_bytes=0, fields=second_fields
    )
    first_fields["b"] = second
    second_fields["a"] = first
    return {"p": Variable(DYN, first), "q": Variable(DYN, second)}


def test_cel_cost_estimate_cuts_whole_cycles_regardless_of_order() -> None:
    environment = cyclic_pair(1000, 1000)

    forward = check("p.all(x, true) && q.a.all(x, true)", environment)
    backward = check("q.a.all(x, true) && p.all(x, true)", environment)

    assert isinstance(forward, CheckedExpression)
    assert isinstance(backward, CheckedExpression)
    assert forward.estimate.steps == backward.estimate.steps
    assert forward.estimate.memory == backward.estimate.memory


def test_cel_cost_estimate_refuses_inconsistent_bounds() -> None:
    larger_child = cyclic_pair(60_000, 10)
    oversized = ValueBound(
        bytes=60_000,
        values=30_000,
        items=1,
        string_bytes=0,
        element=ValueBound(bytes=100, values=10, items=50_000, string_bytes=0),
    )
    negative = ValueBound(bytes=-1, values=1, items=0, string_bytes=0)
    not_a_number = ValueBound(bytes=1, values=1, items=0, string_bytes=0)
    object.__setattr__(not_a_number, "bytes", float("nan"))

    with pytest.raises(BoundError, match="larger than its parent"):
        check("p == p", larger_child)
    with pytest.raises(BoundError, match="exceeds the data bounds"):
        check("v[0] == 1", {"v": Variable(DYN, oversized)})
    with pytest.raises(BoundError, match="negative"):
        check("v == 1", {"v": Variable(DYN, negative)})
    with pytest.raises(BoundError, match="not an integer"):
        check("v == 1", {"v": Variable(DYN, not_a_number)})


def test_cel_cost_estimate_never_charges_dyn_additions_below_typed_ones() -> None:
    small = ValueBound(bytes=3, values=1, items=0, string_bytes=0)
    additions = "[" + ", ".join(["a + b"] * 50) + "] == [a]"

    untyped = check(additions, {"a": Variable(DYN, small), "b": Variable(DYN, small)})
    typed = check(
        additions, {"a": Variable(DOUBLE, small), "b": Variable(DOUBLE, small)}
    )

    assert isinstance(untyped, CheckedExpression)
    assert isinstance(typed, CheckedExpression)
    assert untyped.estimate.memory >= typed.estimate.memory


class DerivedBound(ValueBound):
    pass


def test_cel_cost_estimate_refuses_malformed_bounds() -> None:
    leaf = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    bad_element = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    object.__setattr__(bad_element, "element", 5)
    bad_fields = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    object.__setattr__(bad_fields, "fields", [leaf])
    bad_root = Variable(DYN, leaf)
    object.__setattr__(bad_root, "bound", 5)
    derived = DerivedBound(bytes=10, values=1, items=0, string_bytes=0)
    first_fields: dict[str, ValueBound] = {}
    second_fields: dict[str, ValueBound] = {}
    first = ValueBound(bytes=10, values=1, items=0, string_bytes=0, fields=first_fields)
    second = ValueBound(
        bytes=10, values=1, items=0, string_bytes=0, fields=second_fields
    )
    first_fields["b"] = second
    second_fields["a"] = first
    object.__setattr__(first, "bytes", "10")
    negative = ValueBound(bytes=-1, values=1, items=0, string_bytes=0)

    bad_field = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    object.__setattr__(bad_field, "fields", {"a": 5})
    bad_name = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    object.__setattr__(bad_name, "fields", {1: leaf})
    derived_field = ValueBound(
        bytes=10, values=1, items=0, string_bytes=0, fields={"a": derived}
    )
    custom_fields = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    object.__setattr__(custom_fields, "fields", ChainMap({"a": leaf}))
    proxied = ValueBound(
        bytes=10,
        values=1,
        items=0,
        string_bytes=0,
        fields=MappingProxyType({"a": leaf}),
    )
    entries = cast("dict[str, Variable]", {"v": leaf})

    unreached = check(
        "v == v", {"v": Variable(DYN, leaf), "w": Variable(DYN, negative)}
    )

    assert isinstance(unreached, CheckedExpression)
    with pytest.raises(BoundError, match="not a plain dict"):
        check("v.a == v.a", {"v": Variable(DYN, proxied)})
    with pytest.raises(BoundError, match="not a plain dict"):
        check(
            "v == v",
            cast("dict[str, Variable]", MappingProxyType({"v": Variable(DYN, leaf)})),
        )
    with pytest.raises(BoundError, match="not a string"):
        check(
            "v == v",
            cast(
                "dict[str, Variable]",
                {"v": Variable(DYN, leaf), 1: Variable(DYN, leaf)},
            ),
        )
    for cel_type in (
        "string",
        7,
        Primitive(cast("Kind", "string")),
        Nullable(cast("CelType", "string")),
    ):
        with pytest.raises(BoundError, match="not a CEL type"):
            check("v + v", {"v": Variable(cast("CelType", cel_type), leaf)})
    nested_nullable = Nullable(Nullable(STRING))
    with pytest.raises(BoundError, match="wraps another nullable"):
        check("v + v", {"v": Variable(nested_nullable, leaf)})
    with pytest.raises(BoundError, match="not a plain dict"):
        check("1 == 1", cast("dict[str, Variable]", MappingProxyType({})))
    broken_record = Record(cast("dict[str, RecordField]", {"a": 1}))
    with pytest.raises(BoundError, match="not a CEL type"):
        check("v.a == 1", {"v": Variable(broken_record, leaf)})
    for malformed in (bad_field, bad_name, derived_field):
        with pytest.raises(BoundError, match="not a value bound"):
            check("v == v", {"v": Variable(DYN, malformed)})
    with pytest.raises(BoundError, match="not a plain dict"):
        check("v == v", {"v": Variable(DYN, custom_fields)})
    with pytest.raises(BoundError, match="not a variable"):
        check("v == v", entries)
    with pytest.raises(BoundError, match="not a value bound"):
        check("v == v", {"v": Variable(DYN, bad_element)})
    with pytest.raises(BoundError, match="not a plain dict"):
        check("v == v", {"v": Variable(DYN, bad_fields)})
    with pytest.raises(BoundError, match="not a value bound"):
        check("v == v", {"v": bad_root})
    with pytest.raises(BoundError, match="not a value bound"):
        check("v == v", {"v": Variable(DYN, derived)})
    for name in ("p", "q"):
        with pytest.raises(BoundError, match="not an integer"):
            check(
                f"{name} == {name}",
                {"p": Variable(DYN, first), "q": Variable(DYN, second)},
            )
