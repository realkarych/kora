from collections.abc import Callable

import pytest

from kora.cel import ValueBound, Variable, check
from kora.cel.meter import Meter
from kora.cel.types import DYN

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

    wide = [environment_steps(width, 1300) for width in widths]
    deep = [shared_depth_steps(depth) for depth in depths]

    for width, count in zip(widths, wide, strict=True):
        assert count <= STEPS_PER_CODE_POINT * (width + 4096)
    for smaller, larger in zip(wide, wide[1:], strict=False):
        assert larger <= 2.5 * smaller
    assert deep[-1] <= 4 * deep[0]
