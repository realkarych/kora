import math
from collections.abc import Iterator
from typing import Literal, Never, cast

import pytest

from kora.ir.structured_output import (
    StructuredOutput,
    StructuredOutputValidationError,
    checked_value,
    parse_structured_output_json,
)
from kora.ir.structured_schema_cursors import (
    build_native_object_index,
    string_equal,
)
from kora.ir.structured_schema_evaluation_policy import PARAMETERS, EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import SchemaArena
from kora.ir.structured_schema_work import PreparationMeter, PreparationRefused
from tests.ir.test_structured_schema_evaluation_work import counters, remaining_meter


def controlled_borrowed_mapping(keys: tuple[str, ...]) -> dict[str, object]:
    model = StructuredOutput({key: [position] for position, key in enumerate(keys)})
    admitted = cast("dict[str, object]", checked_value(model))
    return {key: admitted[key] for key in keys}


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        pytest.param((), (), id="empty"),
        pytest.param(("a",), ("a",), id="singleton"),
        pytest.param(("c", "b", "a"), ("a", "b", "c"), id="reverse-nonpower"),
        pytest.param(
            (
                "prefix\U0001f600",
                "prefix\uffff",
                "prefix\u00e9",
                "prefix\x00",
                "prefix",
            ),
            (
                "prefix",
                "prefix\x00",
                "prefix\u00e9",
                "prefix\uffff",
                "prefix\U0001f600",
            ),
            id="shared-prefix-codepoints",
        ),
        pytest.param(
            ("p" * 1024 + "c", "p" * 1024 + "b", "p" * 1024 + "a"),
            ("p" * 1024 + "a", "p" * 1024 + "b", "p" * 1024 + "c"),
            id="long-shared-prefix",
        ),
    ],
)
def test_native_index_collects_borrowed_rows_with_exact_bulk_and_header_charges(
    keys: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    model = StructuredOutput({key: [position] for position, key in enumerate(keys)})
    admitted = cast("dict[str, object]", checked_value(model))
    mapping = {key: admitted[key] for key in keys}
    meter = EvaluationMeter()
    count = len(keys)
    key_points = sum(len(key) for key in keys)
    passes = (max(1, count) - 1).bit_length()
    collection_work = 1 + 3 * count
    sort_work = 1 + 2 * passes * (count + key_points)
    fixed_administration_work = 2 + 1 + 1 + 3

    index = build_native_object_index(mapping, meter)
    built = counters(meter)
    rows = tuple(index.row(position, meter) for position in range(count))

    assert tuple(key for key, _ in rows) == expected
    assert tuple(mapping) == keys
    assert all(admitted[key] is value for key, value in rows)
    assert admitted == {key: [position] for position, key in enumerate(keys)}
    assert built == (
        fixed_administration_work + collection_work + sort_work,
        0,
        3 + 2 * count + 2 * count,
    )
    assert counters(meter) == (built[0] + 2 * count, 0, built[2])


@pytest.mark.parametrize("query", ["", "a", "b", "c", "d", "\U0001f600"])
def test_native_index_lookup_uses_prepaid_binary_search_and_no_new_entries(
    query: str,
) -> None:
    mapping = controlled_borrowed_mapping(("c", "a", "b"))
    meter = EvaluationMeter()
    index = build_native_object_index(mapping, meter)
    before = counters(meter)
    expected_position = {"a": 0, "b": 1, "c": 2}.get(query, -1)
    count = 3
    search_work = 1 + (count.bit_length() + 1) * (1 + len(query) + 1)

    position = index.find(query, meter)

    assert position == expected_position
    assert counters(meter) == (before[0] + 4 + search_work, 0, before[2])


def test_native_index_empty_lookup_uses_only_empty_search_charge() -> None:
    meter = EvaluationMeter()
    index = build_native_object_index(controlled_borrowed_mapping(()), meter)
    before = counters(meter)

    position = index.find("absent", meter)

    assert position == -1
    assert counters(meter) == (before[0] + 4 + 1, 0, before[2])


@pytest.mark.parametrize("dimension", ["work", "entries"])
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_native_index_reserves_fixed_headers_and_variable_cells_before_allocation(
    dimension: Literal["work", "entries"], short: bool
) -> None:
    mapping = controlled_borrowed_mapping(("c", "b", "a"))
    required_work = 7 + (1 + 3 * 3) + (1 + 2 * 2 * (3 + 3))
    required_entries = 3 + 2 * 3 + 2 * 3
    remaining = required_work if dimension == "work" else required_entries
    meter = remaining_meter(dimension, remaining - int(short))
    before = counters(meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            build_native_object_index(mapping, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert counters(meter) == (
            before[0] + required_work - 3,
            0,
            before[2] + required_entries - 1,
        )
        return
    index = build_native_object_index(mapping, meter)

    assert counters(meter) == (
        before[0] + required_work,
        0,
        before[2] + required_entries,
    )
    assert repr(index) == "NativeObjectIndex()"


@pytest.mark.parametrize(
    ("remaining", "paid_work", "paid_entries"),
    [(0, 0, 0), (3, 3, 1), (13, 13, 7), (14, 14, 8), (39, 39, 14)],
)
def test_native_index_failed_reservations_keep_only_the_paid_prefix(
    remaining: int, paid_work: int, paid_entries: int
) -> None:
    mapping = controlled_borrowed_mapping(("c", "b", "a"))
    meter = remaining_meter("work", remaining)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        build_native_object_index(mapping, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert counters(meter) == (before[0] + paid_work, 0, paid_entries)


@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_native_index_lookup_precharges_its_complete_binary_search(short: bool) -> None:
    index = build_native_object_index(
        controlled_borrowed_mapping(("c", "b", "a")), EvaluationMeter()
    )
    query = "b"
    count = 3
    work = 4 + 1 + (count.bit_length() + 1) * (1 + len(query) + 1)
    meter = remaining_meter("work", work - int(short))
    before = counters(meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            index.find(query, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert counters(meter) == (before[0] + 4, 0, 0)
        return
    position = index.find(query, meter)

    assert position == 1
    assert counters(meter) == (PARAMETERS["max_evaluation_work"], 0, 0)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("", "", True),
        ("same\x00\U0001f600", "same\x00\U0001f600", True),
        ("prefix\u00e9", "prefixe\u0301", False),
        ("a" * 2048 + "b", "a" * 2048 + "c", False),
    ],
)
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_native_string_comparison_reserves_one_codepoint_comparison(
    left: str, right: str, expected: bool, short: bool
) -> None:
    work = 1 + min(len(left), len(right))
    meter = remaining_meter("work", work - int(short))
    before = counters(meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            string_equal(left, right, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert counters(meter) == before
        return
    result = string_equal(left, right, meter)

    assert result is expected
    assert counters(meter) == (PARAMETERS["max_evaluation_work"], 0, 0)


class AuthoredString(str):
    def __len__(self) -> int:
        raise AssertionError("authored text length invoked")

    def __str__(self) -> str:
        raise AssertionError("authored text conversion invoked")

    def __repr__(self) -> str:
        raise AssertionError("private-equality-text-canary")


class AuthoredInteger(int):
    def bit_length(self) -> int:
        raise AssertionError("authored integer inspection invoked")


class AuthoredFloat(float):
    def __repr__(self) -> str:
        raise AssertionError("authored float spelling invoked")


class AuthoredArray(list[object]):
    def __len__(self) -> int:
        raise AssertionError("authored array length invoked")

    def __iter__(self) -> Iterator[object]:
        raise AssertionError("authored array iteration invoked")


class AuthoredObject(dict[str, object]):
    def __len__(self) -> int:
        raise AssertionError("authored object length invoked")

    def items(self) -> Never:
        raise AssertionError("authored object members invoked")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(AuthoredString("text"), id="text-subclass"),
        pytest.param({AuthoredString("key"): None}, id="key-subclass"),
        pytest.param(AuthoredInteger(1), id="integer-subclass"),
        pytest.param(AuthoredFloat(0.3), id="float-subclass"),
        pytest.param(AuthoredArray([None]), id="array-subclass"),
        pytest.param(AuthoredObject({"key": None}), id="object-subclass"),
        pytest.param(math.nan, id="nonfinite"),
        pytest.param("\ud800", id="surrogate-text"),
        pytest.param({"\udfff": None}, id="surrogate-key"),
    ],
)
def test_actual_model_and_arena_admission_refuse_hostile_domain_without_callbacks(
    value: object,
) -> None:
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(StructuredOutputValidationError) as model_rejected:
        StructuredOutput(value)
    with pytest.raises(PreparationRefused) as arena_rejected:
        arena.snapshot(value)

    assert model_rejected.value.violation.code == "SO003"
    assert arena_rejected.value.reason == "unsupported_schema"
    assert "private-equality-text-canary" not in str(model_rejected.value)
    assert "private-equality-text-canary" not in str(arena_rejected.value)


def test_actual_model_and_arena_admission_refuse_cycles() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(StructuredOutputValidationError) as model_rejected:
        StructuredOutput(cycle)
    with pytest.raises(PreparationRefused) as arena_rejected:
        arena.snapshot(cycle)

    assert model_rejected.value.violation.code == "SO003"
    assert arena_rejected.value.reason == "unsupported_schema"


@pytest.mark.parametrize(
    "raw",
    [rb'{"a":0,"\u0061":1}', rb'"\ud800"', rb'{"\udfff":null}'],
)
def test_actual_raw_admission_refuses_duplicate_names_and_surrogate_text(
    raw: bytes,
) -> None:
    candidate = raw

    with pytest.raises(StructuredOutputValidationError) as rejected:
        parse_structured_output_json(candidate)

    assert rejected.value.violation.code == "SO002"
