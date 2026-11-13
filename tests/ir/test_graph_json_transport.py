import math

import pytest

from kora.ir.graph_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.ir.json import JsonObject


@pytest.mark.parametrize(
    "value",
    [
        {"unicode": "é😀", "nul\0key": '\0\b\f\n\r\t"\\'},
        {"surrogates": ["\ud800", "\udfff"], "\ud800": "key"},
        {"wide": 1 << 20_000, "minimum": -(1 << 20_000)},
        {"numbers": [0, 0.0, -0.0, 1, 1.0, 1e-10, True, False, None]},
        {"second": {}, "first": [{"z": 1, "a": 2}, {"omitted": False}]},
    ],
)
def test_utf8_graph_transport_preserves_value_domain_and_exact_budget(
    value: JsonObject,
) -> None:
    allowance = 100_000
    budget = TextBudget(allowance, ascii_only=False)

    encoded = render_object(value, budget)
    acquired = ObjectReader(encoded).object()
    exact = TextBudget(len(encoded.encode("utf-8")), ascii_only=False)
    replay = render_object(value, exact)
    with pytest.raises(GraphValueRejected) as caught:
        render_object(
            value, TextBudget(len(encoded.encode("utf-8")) - 1, ascii_only=False)
        )

    assert acquired == value
    assert list(acquired) == list(value)
    assert replay == encoded
    assert allowance - budget.remaining == len(encoded.encode("utf-8"))
    assert exact.remaining == 0
    assert caught.value.reason is GraphValueFailure.BUDGET
    if "numbers" in acquired:
        numbers = acquired["numbers"]
        assert isinstance(numbers, list)
        assert tuple(map(type, numbers)) == (
            int,
            float,
            float,
            int,
            float,
            float,
            bool,
            bool,
            type(None),
        )
        zero = numbers[2]
        assert type(zero) is float
        assert math.copysign(1, zero) == -1


def test_utf8_graph_transport_escapes_only_required_characters() -> None:
    value: JsonObject = {"text": 'é😀\ud800\0"\\'}

    encoded = render_object(value, TextBudget(100, ascii_only=False))

    assert encoded == '{"text":"é😀\\ud800\\u0000\\"\\\\"}'


def test_utf8_graph_transport_keeps_deep_admitted_graph_values() -> None:
    value: JsonObject = {"leaf": -0.0}
    for _ in range(256):
        value = {"nested": value}

    encoded = render_object(value, TextBudget(4096, ascii_only=False))
    decoded = ObjectReader(encoded).object()

    current = decoded
    for _ in range(256):
        child = current["nested"]
        assert isinstance(child, dict)
        current = child
    leaf = current["leaf"]
    assert type(leaf) is float
    assert math.copysign(1, leaf) == -1


def test_utf8_graph_transport_charges_repeated_occurrences_and_refuses_cycles() -> None:
    child: JsonObject = {"text": "é" * 20}
    value: JsonObject = {"one": child, "two": child}
    cyclic: JsonObject = {}
    cyclic["self"] = cyclic

    encoded = render_object(value, TextBudget(1000, ascii_only=False))
    with pytest.raises(GraphValueRejected) as caught:
        render_object(cyclic, TextBudget(1000, ascii_only=False))

    assert encoded.count("é") == 40
    assert caught.value.reason is GraphValueFailure.INVALID
