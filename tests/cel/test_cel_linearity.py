from collections.abc import Callable

import pytest

from kora.cel import check
from kora.cel.meter import Meter

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
        "[1].all(a, [2].all(b, " + "a && " * ((size - 40) // 5) + "b))"
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
