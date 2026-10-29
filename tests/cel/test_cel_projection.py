import json
import math
from typing import cast

import pytest

import kora.cel as cel
from kora.cel.meter import Meter
from kora.cel.types import DYN, INT, STRING, ListOf, MapOf, Record, RecordField


def projection_environment() -> cel.Environment:
    child = Record({"a": RecordField(INT, True), "b": RecordField(INT, True)})
    record = Record(
        {
            "x": RecordField(INT, True),
            "y": RecordField(INT, False),
            "child": RecordField(child, True),
            "l": RecordField(ListOf(child), True),
            "m": RecordField(MapOf(STRING, INT), True),
        }
    )
    bound = cel.ValueBound(
        4096, 256, 1000, 16384, fields={"l": cel.ValueBound(4096, 256, 4, 16384)}
    )
    return cel.Environment(
        {"r": cel.Variable(record, bound), "p": cel.Variable(DYN)}, cel.ResultKind.BOOL
    )


READS: list[tuple[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]]] = [
    ("r.x > 0", {"r": ((".x",), ())}),
    ("has(r.y)", {"r": ((), (".y",))}),
    ("has(r.y) && r.y > 0", {"r": ((".y",), ())}),
    ("'k' in r.m", {"r": ((), (".m['k']",))}),
    ("'k' in r.m && r.m['k'] > 0", {"r": ((".m['k']",), ())}),
    ("'a' in r.child", {"r": ((), (".child.a",))}),
    ("r.l[0].a > 0", {"r": ((".l[*].a",), ())}),
    ("r.l[0].a == r.l[1].b", {"r": ((".l[*].a", ".l[*].b"), ())}),
    ("r.l[r.x].a > 0", {"r": ((".l", ".x"), ())}),
    ("r.l.all(x, x.a > r.x)", {"r": ((".l", ".x"), ())}),
    ("r.l.all(x, r.l.exists(x, x.a > r.x))", {"r": ((".l", ".x"), ())}),
    ("r.child == r.child", {"r": ((".child",), ())}),
    ("size(r.child) > 0", {"r": ((".child",), ())}),
    ("p[0].a == 1", {"p": (("",), ())}),
    ("p[r.x].a == 1", {"p": (("",), ()), "r": ((".x",), ())}),
    ("'k' in p", {"p": (("",), ())}),
    ("has(p.a.b)", {"p": ((), (".a.b",))}),
    ("has(p.a) && p.a.b > 0", {"p": ((".a.b",), (".a",))}),
    ("p.a == p.a && has(p.a.b)", {"p": ((".a",), ())}),
    ("true", {}),
]


@pytest.mark.parametrize(("source", "expected"), READS)
@pytest.mark.parametrize("prepared", [False, True])
def test_cel_projection_path_sets_follow_reads(
    source: str,
    expected: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
    prepared: bool,
) -> None:
    environment = projection_environment()

    checked = cel.check_typed(
        source, cel.prepare(environment) if prepared else environment
    )

    assert isinstance(checked, cel.CheckedExpression), checked
    assert {
        name: (paths.whole, paths.presence)
        for name, paths in checked.projections.items()
    } == expected


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".a.b",
        "['a.b']",
        ".a[*]['k']",
        "[*].a",
        "['']",
        "['in']",
        "['a\\'b']",
        "['\\\\']",
        "['\\u0000']",
        "['\\U0001f600']",
    ],
)
def test_cel_projection_notation_round_trips(path: str) -> None:
    meter = Meter()

    steps = cel.parse_projection_path(path, meter)
    rendering = cel.render_projection_path(steps, meter)
    paths = cel.PathSet((rendering,), ())

    assert rendering == path
    assert paths.whole == (path,)
    assert paths.presence == ()
    assert meter.steps >= len(path)


@pytest.mark.parametrize("name", ["in", "true", "null", "if", "😀" * 500])
def test_cel_projection_generated_keys_round_trip(name: str) -> None:
    source = (
        "'" + name + "' in r.m"
        if name.isascii()
        else "p['" + name + "'] == p['" + name + "']"
    )

    checked = cel.check_typed(source, projection_environment())

    assert isinstance(checked, cel.CheckedExpression), checked
    for paths in checked.projections.values():
        for path in (*paths.whole, *paths.presence):
            assert cel.render_projection_path(cel.parse_projection_path(path)) == path


@pytest.mark.parametrize(
    "path", ["a", ".", "[0]", "[-1]", "[x]", "[**]", "['a'", ".a()", "['\\ud800']"]
)
def test_cel_projection_notation_refuses_malformed_paths(path: str) -> None:
    from kora.cel.diagnostics import DiagnosticError

    with pytest.raises(DiagnosticError):
        cel.parse_projection_path(path)


def test_cel_projection_notation_refuses_oversized_numeric_paths() -> None:
    from kora.cel.diagnostics import DiagnosticError

    path = "[" + "9" * 5000 + "]"

    with pytest.raises(DiagnosticError) as caught:
        cel.parse_projection_path(path)

    assert caught.value.diagnostic.reason is cel.Reason.INVALID_PATH
    assert caught.value.diagnostic.code is cel.Code.SYNTAX
    assert caught.value.diagnostic.offset == 0
    assert str(caught.value) == "invalid_path"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_cel_projection_whole_paths_subsume_deeper_reads() -> None:
    paths = cel.PathSet((".a.b", ".a", ".c.x"), (".a", ".a.d", ".c"))

    result = cel.cut({"a": {"b": 1, "d": 2}, "c": {"x": 3, "unused": 4}}, paths)

    assert paths.whole == (".a", ".c.x")
    assert paths.presence == (".c",)
    assert result == {"a": {"b": 1, "d": 2}, "c": {"x": 3}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", ""),
        (7, 0),
        (3.5, 0.0),
        (True, True),
        (False, False),
        (None, None),
        ([1, 2], []),
        ({"x": 1}, {}),
    ],
)
def test_cel_projection_cut_keeps_only_read_paths(
    value: cel.JsonValue, expected: cel.JsonValue
) -> None:
    original: cel.JsonValue = {
        "keep": {"deep": 7, "unused": 9},
        "present": value,
        "unused": "payload",
    }
    paths = cel.PathSet((".keep.deep",), (".present",))

    result = cel.cut(original, paths)

    assert result == {"keep": {"deep": 7}, "present": expected}
    assert isinstance(result, dict)
    assert type(result["present"]) is type(expected)


def test_cel_projection_cut_preserves_list_positions_and_kind_mismatches() -> None:
    original: cel.JsonValue = {
        "l": [{"x": -0.0, "extra": 2}, {}, 7, "s", True, None, []]
    }
    paths = cel.PathSet((".l[*].x",), ())

    result = cel.cut(original, paths)

    assert result == {"l": [{"x": -0.0}, {}, 0, "", True, None, []]}
    assert isinstance(result, dict)
    assert isinstance(result["l"], list)
    first = result["l"][0]
    assert isinstance(first, dict)
    zero = first["x"]
    assert isinstance(zero, float)
    assert math.copysign(1.0, zero) == -1.0
    assert result is not original
    assert result["l"] is not original["l"]


@pytest.mark.parametrize("count", [1000, 10000])
def test_cel_projection_cut_accepts_a_large_original_with_a_small_projection(
    count: int,
) -> None:
    original: cel.JsonValue = {"keep": 1, "unused": ["x" * 100 for _ in range(count)]}
    meter = Meter()
    paths = cel.PathSet((".keep",), ())

    result = cel.cut(original, paths, meter)

    assert result == {"keep": 1}
    assert meter.steps < 128


@pytest.mark.parametrize("count", [8, 16, 32, 64])
def test_cel_projection_cut_is_linear_and_never_grows(count: int) -> None:
    original: cel.JsonValue = {
        "l": [{"x": index, "unused": "text"} for index in range(count)]
    }
    meter = Meter()
    paths = cel.PathSet((".l[*].x",), ())

    result = cel.cut(original, paths, meter)

    assert len(json.dumps(result)) <= len(json.dumps(original))
    assert count <= meter.steps <= 128 * count + 128


@pytest.mark.parametrize(
    "value", ["x" * 4096, [0] * 256, {str(index): 0 for index in range(1001)}]
)
def test_cel_projection_cut_refuses_oversized_output(value: cel.JsonValue) -> None:
    meter = Meter()
    paths = cel.PathSet(("",), ())

    with pytest.raises(cel.BoundError):
        cel.cut(value, paths, meter)

    assert meter.steps < 10000


def test_cel_projection_cut_refuses_cycles_and_excessive_depth() -> None:
    cycle: list[cel.JsonValue] = []
    cycle.append(cycle)
    deep: cel.JsonValue = 1
    for _ in range(33):
        deep = [deep]
    paths = cel.PathSet(("",), ())

    with pytest.raises(cel.BoundError):
        cel.cut(cycle, paths)
    with pytest.raises(cel.BoundError):
        cel.cut(deep, paths)


def test_cel_projection_untyped_checks_have_no_path_sets() -> None:
    checked = cel.check("p.a == 1")

    assert isinstance(checked, cel.CheckedExpression)
    assert checked.projections == {}
    assert checked.projection_bytes == 0
    with pytest.raises(TypeError):
        cast("dict[str, object]", checked.projections)["new"] = object()


@pytest.mark.parametrize("attribute", ["whole", "presence"])
def test_cel_projection_metadata_is_detached_immutable_and_charged(
    attribute: str,
) -> None:
    from dataclasses import FrozenInstanceError

    environment = projection_environment()
    checked = cel.check_typed("has(p.x)", environment)

    assert isinstance(checked, cel.CheckedExpression)
    paths = checked.projections["p"]

    assert checked.projection_bytes == 664
    assert paths.whole == ()
    assert paths.presence == (".x",)
    with pytest.raises(FrozenInstanceError):
        setattr(paths, attribute, ("",))
    with pytest.raises(TypeError):
        cast("dict[str, object]", checked.projections)["new"] = object()


class DerivedProjectionList(list[cel.JsonValue]):
    pass


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), "\ud800", DerivedProjectionList([1])]
)
def test_cel_projection_cut_refuses_non_json_values(value: cel.JsonValue) -> None:
    paths = cel.PathSet(("",), ())

    with pytest.raises(cel.BoundError):
        cel.cut(value, paths)


@pytest.mark.parametrize("prefix", ["shared_prefix_", "😀" * 16])
def test_cel_projection_extraction_counts_prefixes_and_sorting_linearly(
    prefix: str,
) -> None:
    environment = cel.Environment(
        {"m": cel.Variable(MapOf(STRING, DYN))}, cel.ResultKind.BOOL
    )
    sources = [
        "["
        + ", ".join("'" + prefix + str(index) + "' in m" for index in range(count))
        + "].all(x, x)"
        for count in (8, 16, 32)
    ]
    counts: list[int] = []

    for source in sources:
        meter = Meter()
        checked = cel.check_typed(source, environment, meter)
        assert isinstance(checked, cel.CheckedExpression), checked
        assert checked.projections["m"].presence == tuple(
            sorted(checked.projections["m"].presence)
        )
        assert meter.steps <= 512 * len(source) + 256
        counts.append(meter.steps)

    for smaller, larger in zip(counts, counts[1:], strict=False):
        assert larger <= 2.6 * smaller


@pytest.mark.parametrize("bits", [100_000, 1_000_000])
def test_cel_projection_refuses_huge_negative_integers_with_bounded_counted_work(
    bits: int,
) -> None:
    value = -(1 << bits)
    paths = cel.PathSet(("",), ())
    meter = Meter()

    with pytest.raises(cel.BoundError):
        cel.cut(value, paths, meter)

    assert 256 < meter.steps < 20_000
