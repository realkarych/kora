from collections.abc import Callable

import pytest

from kora.cel import Environment, ResultKind, ValueBound, Variable, check, check_typed
from kora.cel.meter import Meter
from kora.cel.types import DYN, INT, Record, RecordField

SIZES: tuple[int, ...] = (512, 1024, 2048, 4000)
STEPS_PER_CODE_POINT: int = 8

FAMILIES: dict[str, Callable[[int], str]] = {
    "parentheses": lambda size: "(" * size,
    "conditionals": lambda size: ("a ? b : " * size)[:size] + "c",
    "additions": lambda size: ("a + " * size)[:size] + "a",
    "selections": lambda size: "a" + ".b" * (size // 2),
    "negations": lambda size: "!" * size + "x",
    "wide_list": lambda size: "[" + ", ".join(["1"] * (size // 3)) + "]",
    "wide_map": lambda size: "{" + ", ".join(["1: 2"] * (size // 6)) + "}",
    "long_string": lambda size: "'" + "a" * (size - 2) + "'",
    "escapes": lambda size: "'" + "\\U0001f600" * (size // 10) + "'",
    "comment": lambda size: "x // " + "c" * size + "\n+ 1",
    "pattern_atoms": lambda size: "x.matches('" + "a" * (size - 14) + "')",
    "pattern_classes": lambda size: "x.matches('" + "[a-z]" * (size // 6) + "')",
    "pattern_groups": lambda size: "x.matches('" + "(" * (size - 14) + "')",
    "pattern_brackets": lambda size: "x.matches('" + "[" * (size - 14) + "')",
    "nested_macros": lambda size: (
        "[1, 2].all(a, [3].all(b, ["
        + ", ".join(["a + b"] * ((size - 40) // 7))
        + "].size() > 0))"
    ),
    "wide_calls": lambda size: "[" + ", ".join(["size(x)"] * (size // 9)) + "]",
}


def steps_for(source: str) -> int:
    meter = Meter()
    check(source, None, meter)
    return meter.steps


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_cel_work_is_linear_in_steps(family: str) -> None:
    build = FAMILIES[family]
    sources = [build(size) for size in SIZES]

    counts = [steps_for(source) for source in sources]

    for source, count in zip(sources, counts, strict=True):
        assert len(source) <= 4096
        assert count <= STEPS_PER_CODE_POINT * len(source)
    for smaller, larger in zip(counts, counts[1:], strict=False):
        assert larger <= 2.5 * smaller


def environment_steps(fields: int, references: int) -> int:
    leaf = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    wide = ValueBound(
        bytes=60_000,
        values=30_000,
        items=1000,
        string_bytes=0,
        fields={f"f{index}": leaf for index in range(fields)},
    )
    meter = Meter()
    source = "[" + ", ".join(["v"] * references) + "]"
    check(source, {"v": Variable(DYN, wide)}, meter)
    return meter.steps


def shared_depth_steps(depth: int) -> int:
    bound = ValueBound(bytes=10, values=1, items=0, string_bytes=0)
    for _ in range(depth):
        bound = ValueBound(
            bytes=60_000,
            values=30_000,
            items=2,
            string_bytes=0,
            fields={"a": bound, "b": bound},
        )
    meter = Meter()
    check("v.a.b.a.b == 1", {"v": Variable(DYN, bound)}, meter)
    return meter.steps


def test_cel_estimate_is_linear_in_the_environment() -> None:
    widths = (1000, 2000, 4000, 8000)
    depths = (8, 16, 32)

    empty = environment_steps(0, 1300)
    wide = [environment_steps(width, 1300) for width in widths]
    deep = [shared_depth_steps(depth) for depth in depths]

    for width, count in zip(widths, wide, strict=True):
        assert count - empty >= width
        assert count <= STEPS_PER_CODE_POINT * (width + 4096)
    for smaller, larger in zip(wide, wide[1:], strict=False):
        assert larger <= 2.5 * smaller
    assert deep[-1] <= 4 * deep[0]


TYPED_STEPS_PER_CODE_POINT: int = 16

TYPED_FAMILIES: dict[str, Callable[[int], str]] = {
    **FAMILIES,
    "guarded_reads": lambda size: (
        "["
        + ", ".join(["has(r.o) && r.o.n > 0"] * ((size - 20) // 23))
        + "].size() > 0"
    ),
    "guard_chains": lambda size: (
        "["
        + ", ".join(
            ["(has(r.o) && has(r.o.o) && has(r.o.o.o)) && r.o.o.o.n > 0"]
            * ((size - 20) // 59)
        )
        + "].size() > 0"
    ),
    "disjunctions": lambda size: (
        "["
        + ", ".join(["!has(r.o) || r.o.n > 0"] * ((size - 20) // 24))
        + "].size() > 0"
    ),
    "long_guard_chains": lambda size: (
        "["
        + ", ".join(
            [" && ".join(["has(r.o)"] * 27) + " && r.o.n > 0"] * ((size - 20) // 340)
        )
        + "].size() > 0"
    ),
    "typed_selections": lambda size: (
        "[" + ", ".join(["r.r.r.r.r.n"] * ((size - 20) // 13)) + "].size() > 0"
    ),
    "integer_negations": lambda size: (
        "[" + ", ".join(["-r.n"] * ((size - 20) // 6)) + "].size() > 0"
    ),
    "dense_integer_negations": lambda size: (
        "[" + ",".join(["-r.n"] * ((size - 20) // 5)) + "].size() > 0"
    ),
    "distinct_map_keys": lambda size: (
        "{" + ", ".join(f"'{index}': 1" for index in range(size // 11)) + "}.size() > 0"
    ),
}


def recursive_record() -> Record:
    fields: dict[str, RecordField] = {}
    record = Record(fields)
    fields["r"] = RecordField(record, True)
    fields["o"] = RecordField(record, False)
    fields["n"] = RecordField(INT, True)
    return record


def typed_steps_for(source: str) -> int:
    meter = Meter()
    variables = {name: Variable(DYN) for name in ("a", "b", "c", "x")}
    variables["r"] = Variable(recursive_record())
    check_typed(source, Environment(variables, ResultKind.BOOL), meter)
    return meter.steps


@pytest.mark.parametrize("family", sorted(TYPED_FAMILIES))
def test_cel_typed_check_is_linear_in_steps(family: str) -> None:
    build = TYPED_FAMILIES[family]
    sources = [build(size) for size in SIZES]

    counts = [typed_steps_for(source) for source in sources]

    for source, count in zip(sources, counts, strict=True):
        assert len(source) <= 4096
        assert count <= TYPED_STEPS_PER_CODE_POINT * len(source)
    for smaller, larger in zip(counts, counts[1:], strict=False):
        assert larger <= 2.5 * smaller
