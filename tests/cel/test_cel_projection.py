import pytest

from kora.cel import (
    EVERY,
    CheckedExpression,
    Field,
    JsonValue,
    PathSet,
    PathSetError,
    check,
    check_typed,
    cut,
    parse_path_set,
)
from kora.cel.meter import Meter
from kora.cel.projection import ProjectionPath, render_path
from tests.cel.projection_support import projection_environment

Rendered = dict[str, dict[str, list[str]]]


def rendered_paths(source: str) -> Rendered:
    environment, _ = projection_environment()
    checked = check_typed(source, environment)
    assert isinstance(checked, CheckedExpression), checked
    return {name: paths.rendered() for name, paths in checked.paths.items()}


def whole(*paths: str) -> dict[str, list[str]]:
    return {"whole": list(paths), "presence": []}


def presence(*paths: str) -> dict[str, list[str]]:
    return {"whole": [], "presence": list(paths)}


READS: list[tuple[str, Rendered]] = [
    ("inputs.review.verdict == 'approved'", {"inputs": whole("review.verdict")}),
    (
        "inputs.bench.cpu < inputs.review.cpu * 0.85",
        {"inputs": whole("bench.cpu", "review.cpu")},
    ),
    ("has(inputs.review.opt)", {"inputs": presence("review.opt")}),
    (
        "has(inputs.review.opt) && inputs.review.opt > 1",
        {"inputs": whole("review.opt")},
    ),
    ("'k' in inputs.review.labels", {"inputs": presence("review.labels.k")}),
    ("'a b' in inputs.review.labels", {"inputs": presence('review.labels["a b"]')}),
    (
        "'k' in inputs.review.labels && inputs.review.labels['k'] == 'v'",
        {"inputs": whole("review.labels.k")},
    ),
    ("inputs.review.tags[1] == 'x'", {"inputs": whole("review.tags[*]")}),
    (
        "inputs.review.notes.exists(n, has(n.score) && n.score > 1)",
        {"inputs": whole("review.notes")},
    ),
    ("inputs.review.tags.size() > 0", {"inputs": whole("review.tags")}),
    ("'x' in inputs.review.tags", {"inputs": whole("review.tags")}),
    (
        "inputs.review.facts == inputs.bench.facts",
        {"inputs": whole("bench.facts", "review.facts")},
    ),
    (
        "has(inputs.review.facts.change) || has(inputs.review.facts.size)",
        {"inputs": presence("review.facts.change", "review.facts.size")},
    ),
    ("payload.a.b == 1", {"payload": whole("a.b")}),
    ("has(payload.a.b)", {"payload": presence("a.b")}),
    ("payload[0] == 1", {"payload": whole("")}),
    ("'k' in payload.m", {"payload": whole("m")}),
    ("payload == {}", {"payload": whole("")}),
    (
        "inputs.review.labels[response.verdict] == 'x'",
        {"inputs": whole("review.labels"), "response": whole("verdict")},
    ),
    (
        "has(payload.a) && payload.a.b == 1 && has(payload.a.c)",
        {"payload": {"whole": ["a.b"], "presence": ["a.c"]}},
    ),
    ("[1, 2].exists(x, x == 1)", {}),
]


@pytest.mark.parametrize(("source", "expected"), READS)
def test_cel_projection_path_sets_follow_reads(source: str, expected: Rendered) -> None:
    paths = rendered_paths(source)

    assert paths == expected


def test_cel_projection_path_sets_are_typed_only() -> None:
    checked = check("payload.a == 1")

    assert isinstance(checked, CheckedExpression)
    assert dict(checked.paths) == {}


NOTATION: list[tuple[ProjectionPath, str]] = [
    ((), ""),
    ((Field("review"), Field("verdict")), "review.verdict"),
    ((Field("artifacts"), Field("implement.change")), 'artifacts["implement.change"]'),
    ((Field("a"), EVERY, Field("b")), "a[*].b"),
    ((EVERY, Field("b")), "[*].b"),
    ((Field("if"), Field("in")), '["if"]["in"]'),
    ((Field('quote"\n'),), '["quote\\"\\n"]'),
    ((Field("\U0001f600"),), '["\\U0001f600"]'),
]


@pytest.mark.parametrize(("path", "text"), NOTATION)
def test_cel_projection_notation_round_trips(path: ProjectionPath, text: str) -> None:
    path_set = PathSet(whole=(path,))

    rendered = path_set.rendered()
    parsed = parse_path_set(rendered["whole"], rendered["presence"])

    assert render_path(path) == text
    assert rendered == {"whole": [text], "presence": []}
    assert parsed == path_set


@pytest.mark.parametrize(
    ("whole_paths", "presence_paths"),
    [
        (["a .b"], []),
        (["a['b']"], []),
        (["a[0]"], []),
        (["b", "a"], []),
        (["a", "a.b"], []),
        (["a"], ["a"]),
        ([], ["a", "a.b"]),
        (["a.b"], ["a"]),
        (["a + b"], []),
        (["a["], []),
    ],
)
def test_cel_projection_notation_refuses_non_canonical_sets(
    whole_paths: list[str], presence_paths: list[str]
) -> None:
    with pytest.raises(PathSetError):
        parse_path_set(whole_paths, presence_paths)


SOURCE: JsonValue = {
    "a": {"b": "long text", "c": [1, 2], "d": True},
    "l": [{"x": "keep", "y": "drop"}, {"x": "also", "z": 1}],
    "s": "scalar",
    "n": None,
    "i": 7,
    "f": 2.5,
    "flag": False,
}
CUTS: list[tuple[PathSet, JsonValue]] = [
    (PathSet(whole=((),)), SOURCE),
    (PathSet(), {}),
    (PathSet(whole=((Field("a"), Field("b")),)), {"a": {"b": "long text"}}),
    (
        PathSet(whole=((Field("l"), EVERY, Field("x")),)),
        {"l": [{"x": "keep"}, {"x": "also"}]},
    ),
    (
        PathSet(
            presence=(
                (Field("a"), Field("b")),
                (Field("a"), Field("c")),
                (Field("a"), Field("d")),
                (Field("n"),),
                (Field("i"),),
                (Field("f"),),
                (Field("flag"),),
                (Field("missing"),),
            )
        ),
        {
            "a": {"b": "", "c": [], "d": True},
            "n": None,
            "i": 0,
            "f": 0.0,
            "flag": False,
        },
    ),
    (PathSet(whole=((Field("s"), Field("x")),)), {"s": ""}),
    (PathSet(whole=((Field("l"), Field("x")),)), {"l": []}),
    (PathSet(whole=((Field("a"), EVERY),)), {"a": {}}),
    (PathSet(whole=((Field("i"), EVERY),)), {"i": 0}),
]


@pytest.mark.parametrize(("path_set", "expected"), CUTS)
def test_cel_projection_cut_keeps_only_read_paths(
    path_set: PathSet, expected: JsonValue
) -> None:
    projected = cut(SOURCE, path_set)

    assert projected == expected
    assert type(projected) is type(expected)


def canonical_size(value: JsonValue) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def wide_value(width: int) -> JsonValue:
    return {
        "items": [{"keep": "x" * 8, "drop": "y" * 8} for _ in range(width)],
        "other": list(range(width)),
    }


def test_cel_projection_cut_is_linear_and_never_grows() -> None:
    widths = (250, 500, 1000, 2000)
    path_set = PathSet(
        whole=((Field("items"), EVERY, Field("keep")),),
        presence=((Field("other"),), (Field("missing"),)),
    )

    meters = [Meter() for _ in widths]
    projected = [
        cut(wide_value(width), path_set, meter)
        for width, meter in zip(widths, meters, strict=True)
    ]

    for width, meter, value in zip(widths, meters, projected, strict=True):
        assert meter.steps <= 3 * width + 8
        assert canonical_size(value) <= canonical_size(wide_value(width))
    for smaller, larger in zip(meters, meters[1:], strict=False):
        assert larger.steps <= 2.5 * smaller.steps
