import gc
import math
import sys
import weakref
from collections import UserDict
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from decimal import Decimal
from typing import Never, cast

import pytest

from kora.ir.structured_schema_input import SchemaArena
from kora.ir.structured_schema_work import (
    PARAMETERS,
    PreparationMeter,
    PreparationRefused,
    binary_lookup,
    merge_sort,
)


class AuthoredString(str):
    def __str__(self) -> str:
        raise AssertionError("authored conversion invoked")


class AuthoredInteger(int):
    def __int__(self) -> int:
        raise AssertionError("authored conversion invoked")


class AuthoredFloat(float):
    def __float__(self) -> float:
        raise AssertionError("authored conversion invoked")


class AuthoredDictionary(dict[str, object]):
    def items(self) -> Never:
        raise AssertionError("authored traversal invoked")


class AuthoredArray(list[object]):
    def __iter__(self) -> Iterator[object]:
        raise AssertionError("authored traversal invoked")


def snapshot(value: object) -> tuple[SchemaArena, int]:
    arena = SchemaArena(PreparationMeter())
    root = arena.snapshot(value, 1)
    return arena, root


def container_ids(value: object) -> set[int]:
    seen: set[int] = set()
    result: set[int] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(item, dict):
            result.add(identity)
            pending.extend(cast("dict[object, object]", item).values())
        elif isinstance(item, list | tuple):
            result.add(identity)
            pending.extend(cast("list[object] | tuple[object, ...]", item))
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
    return result


def test_schema_primitives_pin_the_ten_approved_preparation_capacities() -> None:
    expected = {
        "max_input_bytes": 1_048_576,
        "max_input_nodes": 16_384,
        "max_input_depth": 32,
        "max_schema_documents": 64,
        "max_schema_positions": 2_048,
        "max_reference_edges": 4_096,
        "max_numeric_bits": 14_000,
        "max_preparation_work": 16_777_216,
        "max_auxiliary_bytes": 4_194_304,
        "max_auxiliary_entries": 32_768,
    }

    actual = dict(PARAMETERS)

    assert actual == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, b"null", id="null"),
        pytest.param(True, b"true", id="true"),
        pytest.param(False, b"false", id="false"),
        pytest.param(0, b"0", id="zero"),
        pytest.param(-1, b"-1", id="negative-integer"),
        pytest.param(0.0, b"0.0", id="double-zero"),
        pytest.param(-0.0, b"-0.0", id="negative-double-zero"),
        pytest.param(1e-7, b"1e-07", id="exponent"),
        pytest.param(5e-324, b"5e-324", id="smallest-subnormal"),
        pytest.param(
            sys.float_info.max, b"1.7976931348623157e+308", id="largest-finite"
        ),
        pytest.param(
            9_007_199_254_740_993, b"9007199254740993", id="exact-large-integer"
        ),
        pytest.param(
            '"\\\b\f\n\r\t\x00', b'"\\"\\\\\\b\\f\\n\\r\\t\\u0000"', id="escapes"
        ),
        pytest.param("aé€🌍", '"aé€🌍"'.encode(), id="unicode-widths"),
        pytest.param([], b"[]", id="empty-array"),
        pytest.param({}, b"{}", id="empty-object"),
        pytest.param(
            {"z": [0, False], "a": None}, b'{"a":null,"z":[0,false]}', id="object-order"
        ),
        pytest.param(
            {"🌍": False, "\x00": True, "a": None},
            '{"\\u0000":true,"a":null,"🌍":false}'.encode(),
            id="unicode-and-nul-keys",
        ),
    ],
)
def test_schema_native_snapshot_has_exact_canonical_bytes(
    value: object, expected: bytes
) -> None:
    arena, root = snapshot(value)

    encoded = arena.encode(root)

    assert encoded == expected
    assert arena.bytes == len(expected)


def test_schema_snapshot_owns_input_and_preserves_alias_occurrences() -> None:
    shared: dict[str, object] = {"value": ["saved", 0]}
    original: list[object] = [shared, shared]
    original_ids = container_ids(original)
    arena, root = snapshot(original)
    nodes = tuple(arena.nodes)

    shared.clear()
    original.clear()
    encoded = arena.encode(root)

    assert encoded == b'[{"value":["saved",0]},{"value":["saved",0]}]'
    assert original_ids.isdisjoint(container_ids(nodes))
    assert len(nodes) == 11
    assert nodes[root].children[0] != nodes[root].children[1]


def test_schema_native_snapshot_has_deterministic_order_and_accounting() -> None:
    first = {"z": {"second": 0, "first": 1}, "a": ["🌍", False]}
    second = {"a": ["🌍", False], "z": {"first": 1, "second": 0}}

    left, left_root = snapshot(first)
    right, right_root = snapshot(second)

    assert left_root == right_root
    assert left.nodes == right.nodes
    assert left.meter == right.meter
    assert left.bytes == right.bytes
    assert left.depth == right.depth


def test_schema_frozen_arena_releases_builder_and_has_content_free_repr() -> None:
    marker = "private-schema-canary"
    original: dict[str, object] = {marker: [marker, {"saved": False}]}
    original_ids = container_ids(original)
    arena, root = snapshot(original)
    frozen = arena.freeze(root)
    meter = PreparationMeter()

    original.clear()
    arena.nodes.clear()
    encoded = frozen.encode(meter)

    assert (
        encoded
        == b'{"private-schema-canary":["private-schema-canary",{"saved":false}]}'
    )
    assert original_ids.isdisjoint(container_ids(frozen))
    assert frozen.input_bytes == len(encoded)
    assert frozen.input_depth == 3
    assert marker not in repr(frozen)
    assert marker not in repr(frozen.nodes)


@pytest.mark.parametrize("delta", [0, 1], ids=["exact", "one-short"])
def test_schema_encoding_reserves_both_owned_byte_buffers(delta: int) -> None:
    arena, root = snapshot("abc")
    frozen = arena.freeze(root)
    meter = PreparationMeter(
        auxiliary_bytes=PARAMETERS["max_auxiliary_bytes"] - 10 + delta
    )

    if delta:
        with pytest.raises(PreparationRefused) as rejected:
            frozen.encode(meter)

        assert rejected.value.reason == "preparation_exhausted"
        assert meter.auxiliary_bytes <= PARAMETERS["max_auxiliary_bytes"]
        return
    encoded = frozen.encode(meter)

    assert encoded == b'"abc"'
    assert meter.auxiliary_bytes == PARAMETERS["max_auxiliary_bytes"]


def test_schema_freeze_reserves_index_before_consuming_builder() -> None:
    meter = PreparationMeter(auxiliary_entries=PARAMETERS["max_auxiliary_entries"])
    arena = SchemaArena(meter)
    root = arena.snapshot("abc", 1)
    original_nodes = tuple(arena.nodes)

    with pytest.raises(PreparationRefused) as rejected:
        arena.freeze(root)

    assert rejected.value.reason == "preparation_exhausted"
    assert tuple(arena.nodes) == original_nodes


def test_schema_multiple_snapshots_share_one_auxiliary_reservation_counter() -> None:
    meter = PreparationMeter(auxiliary_entries=PARAMETERS["max_auxiliary_entries"] - 4)
    arena = SchemaArena(meter)

    first = arena.snapshot({}, 1)
    second = arena.snapshot({}, 1)
    with pytest.raises(PreparationRefused) as rejected:
        arena.snapshot({}, 1)

    assert first != second
    assert rejected.value.reason == "preparation_exhausted"
    assert meter.auxiliary_entries == PARAMETERS["max_auxiliary_entries"]


@pytest.mark.parametrize("kind", ["array", "object"], ids=str)
def test_schema_len_detectable_collection_refuses_before_child_visits(
    kind: str,
) -> None:
    value: object = (
        [AuthoredInteger(1)] * PARAMETERS["max_input_nodes"]
        if kind == "array"
        else {str(index): AuthoredInteger(1) for index in range(8_192)}
    )
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(PreparationRefused) as rejected:
        arena.snapshot(value, 1)

    assert rejected.value.reason == "preparation_exhausted"
    assert not arena.nodes
    assert arena.meter.auxiliary_entries == 0


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(AuthoredString("canary"), id="string-subclass"),
        pytest.param(AuthoredInteger(1), id="integer-subclass"),
        pytest.param(AuthoredFloat(1), id="float-subclass"),
        pytest.param(AuthoredDictionary(), id="dictionary-subclass"),
        pytest.param(AuthoredArray(), id="array-subclass"),
        pytest.param(UserDict({"value": 1}), id="mapping"),
        pytest.param(Decimal("1"), id="decimal"),
        pytest.param((1, 2), id="tuple"),
        pytest.param(b"canary", id="bytes"),
        pytest.param({1: "value"}, id="non-string-key"),
        pytest.param({AuthoredString("key"): 1}, id="key-subclass"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="infinity"),
        pytest.param(-math.inf, id="negative-infinity"),
        pytest.param("\ud800", id="surrogate-value"),
        pytest.param({"\udfff": 1}, id="surrogate-key"),
    ],
)
def test_schema_native_snapshot_refuses_without_authored_coercion(
    value: object,
) -> None:
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(PreparationRefused) as rejected:
        arena.snapshot({"annotation": value}, 1)

    assert rejected.value.reason == "unsupported_schema"


@pytest.mark.parametrize("entry", ["scalar", "snapshot"])
@pytest.mark.parametrize("behavior", ["raise", "impersonate"])
def test_schema_native_kind_checks_do_not_invoke_authored_metaclasses(
    entry: str, behavior: str
) -> None:
    calls: list[str] = []
    marker = "authored-metaclass-canary"

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("equality")
            if behavior == "raise":
                raise AssertionError(marker)
            return True

    class Authored(metaclass=AuthoredMeta):
        def __bool__(self) -> bool:
            calls.append("truthiness")
            raise AssertionError(marker)

    value = Authored()
    retained = weakref.ref(value)
    arena = SchemaArena(PreparationMeter())
    operation = arena.scalar if entry == "scalar" else arena.snapshot

    with pytest.raises(PreparationRefused) as rejected:
        operation(value)
    reason = rejected.value.reason
    del rejected
    del value
    gc.collect()

    assert reason == "unsupported_schema"
    assert calls == []
    assert retained() is None
    assert not arena.nodes


@pytest.mark.parametrize("kind", ["array", "object"], ids=str)
def test_schema_native_snapshot_rejects_active_ancestor_cycles(kind: str) -> None:
    array: list[object] = []
    mapping: dict[str, object] = {}
    value: object = array if kind == "array" else mapping
    array.append(array)
    mapping["child"] = mapping
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(PreparationRefused) as rejected:
        arena.snapshot(value, 1)

    assert rejected.value.reason == "unsupported_schema"


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_schema_native_snapshot_enforces_inclusive_byte_capacity(delta: int) -> None:
    size = PARAMETERS["max_input_bytes"] + delta
    value = "x" * (size - 2)
    arena = SchemaArena(PreparationMeter())

    if delta > 0:
        with pytest.raises(PreparationRefused) as rejected:
            arena.snapshot(value, 1)

        assert rejected.value.reason == "preparation_exhausted"
        assert not arena.nodes
        return
    root = arena.snapshot(value, 1)
    encoded = arena.encode(root)

    assert arena.bytes == size
    assert len(encoded) == size


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_schema_native_snapshot_enforces_inclusive_node_capacity(delta: int) -> None:
    count = PARAMETERS["max_input_nodes"] - 1 + delta
    value: list[object] = [None] * count
    if delta > 0:
        value[0] = AuthoredString("must-not-be-visited")
    arena = SchemaArena(PreparationMeter())

    if delta > 0:
        with pytest.raises(PreparationRefused) as rejected:
            arena.snapshot(value, 1)

        assert rejected.value.reason == "preparation_exhausted"
        assert not arena.nodes
        return
    root = arena.snapshot(value, 1)
    encoded = arena.encode(root)

    assert len(arena.nodes) == count + 1
    assert len(encoded) == 1 + 5 * count


@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "over"])
def test_schema_native_snapshot_enforces_inclusive_container_depth(delta: int) -> None:
    depth = PARAMETERS["max_input_depth"] + delta
    value: object = []
    for _ in range(depth - 1):
        value = [value]
    arena = SchemaArena(PreparationMeter())

    if delta > 0:
        with pytest.raises(PreparationRefused) as rejected:
            arena.snapshot(value, 1)

        assert rejected.value.reason == "preparation_exhausted"
        assert arena.depth <= PARAMETERS["max_input_depth"]
        return
    root = arena.snapshot(value, 1)
    encoded = arena.encode(root)

    assert arena.depth == depth
    assert encoded == b"[" * depth + b"]" * depth


@pytest.mark.parametrize("sign", [-1, 1], ids=["negative", "positive"])
@pytest.mark.parametrize("bits", [13_999, 14_000, 14_001], ids=["below", "at", "over"])
def test_schema_native_snapshot_checks_integer_bits_before_conversion(
    sign: int, bits: int
) -> None:
    value = sign * ((1 << bits) - 1)
    arena = SchemaArena(PreparationMeter())
    digit_limit = sys.get_int_max_str_digits()

    if bits > PARAMETERS["max_numeric_bits"]:
        with pytest.raises(PreparationRefused) as rejected:
            arena.snapshot(value, 1)

        assert rejected.value.reason == "unsupported_schema"
        assert not arena.nodes
        assert arena.meter.auxiliary_bytes == 0
        return
    root = arena.snapshot(value, 1)
    encoded = arena.encode(root)

    assert encoded == str(value).encode()
    assert sys.get_int_max_str_digits() == digit_limit


@pytest.mark.parametrize(
    ("value", "text", "work", "byte_count", "entries"),
    [
        pytest.param(0, "0", 4, 1, 0, id="zero"),
        pytest.param(1, "1", 30, 24, 1, id="one"),
        pytest.param(-1, "-1", 35, 26, 2, id="negative-one"),
        pytest.param(10**9, "1000000000", 54, 50, 2, id="zero-low-chunk"),
        pytest.param(-(10**9), "-1000000000", 60, 55, 3, id="signed-zero-low-chunk"),
        pytest.param(10**18, "1000000000000000000", 80, 80, 3, id="two-zero-chunks"),
        pytest.param(
            10**18 + 1, "1000000000000000001", 80, 80, 3, id="zero-middle-chunk"
        ),
    ],
)
def test_schema_integer_primitive_has_exact_signed_chunk_reservations(
    value: int, text: str, work: int, byte_count: int, entries: int
) -> None:
    meter = PreparationMeter()

    actual = meter.integer(value)

    assert actual == text
    assert meter.work == work
    assert meter.auxiliary_bytes == byte_count
    assert meter.auxiliary_entries == entries


@pytest.mark.parametrize("dimension", ["work", "bytes", "entries"])
def test_schema_meter_reservation_is_inclusive_and_atomic(dimension: str) -> None:
    limit = PARAMETERS[
        {
            "work": "max_preparation_work",
            "bytes": "max_auxiliary_bytes",
            "entries": "max_auxiliary_entries",
        }[dimension]
    ]
    meter = PreparationMeter()
    reservation = {"work": 1, "byte_count": 2, "entries": 3}
    key = {"work": "work", "bytes": "byte_count", "entries": "entries"}[dimension]
    reservation[key] = limit

    meter.reserve(**reservation)
    before = (meter.work, meter.auxiliary_bytes, meter.auxiliary_entries)
    with pytest.raises(PreparationRefused) as rejected:
        meter.reserve(**{"work": 0, "byte_count": 0, "entries": 0, key: 1})

    assert rejected.value.reason == "preparation_exhausted"
    assert (meter.work, meter.auxiliary_bytes, meter.auxiliary_entries) == before


@pytest.mark.parametrize("dimension", ["work", "bytes"])
@pytest.mark.parametrize("delta", [0, 1], ids=["exact", "one-short"])
def test_schema_integer_primitive_uses_shared_monotone_capacity(
    dimension: str, delta: int
) -> None:
    meter = PreparationMeter(
        work=PARAMETERS["max_preparation_work"] - 54 + delta
        if dimension == "work"
        else 0,
        auxiliary_bytes=PARAMETERS["max_auxiliary_bytes"] - 50 + delta
        if dimension == "bytes"
        else 0,
    )

    if delta:
        with pytest.raises(PreparationRefused) as rejected:
            meter.integer(10**9)

        assert rejected.value.reason == "preparation_exhausted"
        assert meter.work <= PARAMETERS["max_preparation_work"]
        assert meter.auxiliary_bytes <= PARAMETERS["max_auxiliary_bytes"]
        return
    result = meter.integer(10**9)

    assert result == "1000000000"
    assert (meter.work if dimension == "work" else meter.auxiliary_bytes) == PARAMETERS[
        "max_preparation_work" if dimension == "work" else "max_auxiliary_bytes"
    ]


@pytest.mark.parametrize(
    ("value", "text", "work"),
    [
        pytest.param(10**18, "1000000000000000000", 80, id="multichunk"),
        pytest.param(-(10**9), "-1000000000", 60, id="negative-sign"),
    ],
)
@pytest.mark.parametrize("dimension", ["entries", "work"])
@pytest.mark.parametrize("delta", [0, 1], ids=["exact", "one-short"])
def test_schema_integer_chunk_and_sign_moves_reserve_before_exceeding_capacity(
    value: int, text: str, work: int, dimension: str, delta: int
) -> None:
    meter = PreparationMeter(
        work=PARAMETERS["max_preparation_work"] - work + delta
        if dimension == "work"
        else 0,
        auxiliary_entries=PARAMETERS["max_auxiliary_entries"] - 3 + delta
        if dimension == "entries"
        else 0,
    )

    if delta:
        with pytest.raises(PreparationRefused) as rejected:
            meter.integer(value)

        assert rejected.value.reason == "preparation_exhausted"
        assert meter.work <= PARAMETERS["max_preparation_work"]
        assert meter.auxiliary_entries <= PARAMETERS["max_auxiliary_entries"]
        return
    result = meter.integer(value)

    assert result == text
    assert (
        meter.work if dimension == "work" else meter.auxiliary_entries
    ) == PARAMETERS[
        "max_preparation_work" if dimension == "work" else "max_auxiliary_entries"
    ]


def test_schema_unicode_scan_and_comparison_charge_codepoints() -> None:
    meter = PreparationMeter()

    size = meter.string_size('aé€🌍\x00\n"\\')
    comparison = meter.compare("🌍a", "🌍b")

    assert size == 24
    assert comparison == -1
    assert meter.work == 12
    assert meter.auxiliary_bytes == 0


def test_schema_reservation_failure_precedes_unicode_scan() -> None:
    meter = PreparationMeter(work=PARAMETERS["max_preparation_work"] - 1)

    with pytest.raises(PreparationRefused) as rejected:
        meter.string_size("\ud800")

    assert rejected.value.reason == "preparation_exhausted"
    assert meter.work == PARAMETERS["max_preparation_work"] - 1


def test_schema_float_primitive_reserves_frozen_conversion_and_result() -> None:
    meter = PreparationMeter()

    text = meter.floating(-0.0)

    assert text == "-0.0"
    assert meter.work == 4_103
    assert meter.auxiliary_bytes == 28


def test_schema_stable_string_sort_reserves_exact_bulk_work_and_scratch() -> None:
    rows = [("bb", 1), ("a", 2), ("bb", 3)]
    meter = PreparationMeter()

    merge_sort(rows, lambda row: row[0], meter)

    assert rows == [("a", 2), ("bb", 1), ("bb", 3)]
    assert meter.work == 33
    assert meter.auxiliary_entries == 6
    assert meter.auxiliary_bytes == 0


def test_schema_fixed_pair_sort_and_integer_lookup_have_exact_charges() -> None:
    rows = [(3, 30), (1, 10), (2, 20)]
    meter = PreparationMeter()

    merge_sort(rows, lambda row: row, meter)
    found = binary_lookup(rows, 2, meter)
    absent = binary_lookup(rows, 4, meter)
    empty = binary_lookup([], 0, meter)

    assert rows == [(1, 10), (2, 20), (3, 30)]
    assert (found, absent, empty) == (20, -1, -1)
    assert meter.work == 32
    assert meter.auxiliary_entries == 6


@pytest.mark.parametrize(
    ("count", "work"),
    [(0, 1), (1, 1), (2, 9), (4, 33), (5, 61)],
    ids=["empty", "singleton", "two", "power-of-two", "next-level"],
)
def test_schema_pair_sort_charges_exact_mergesort_levels(count: int, work: int) -> None:
    rows = [(index, 0) for index in reversed(range(count))]
    meter = PreparationMeter()

    merge_sort(rows, lambda row: row, meter)

    assert rows == [(index, 0) for index in range(count)]
    assert meter.work == work
    assert meter.auxiliary_entries == 2 * count


@pytest.mark.parametrize("dimension", ["work", "entries"])
def test_schema_sort_reserves_before_mutating_rows(dimension: str) -> None:
    rows = [("bb", 1), ("a", 2), ("bb", 3)]
    expected = rows.copy()
    meter = PreparationMeter(
        work=PARAMETERS["max_preparation_work"] - 32 if dimension == "work" else 0,
        auxiliary_entries=PARAMETERS["max_auxiliary_entries"] - 5
        if dimension == "entries"
        else 0,
    )

    with pytest.raises(PreparationRefused) as rejected:
        merge_sort(rows, lambda row: row[0], meter)

    assert rejected.value.reason == "preparation_exhausted"
    assert rows == expected


def test_schema_object_lookup_reuses_sorted_rows_and_charges_whole_operation() -> None:
    arena, root = snapshot({"bb": False, "a": True, "ccc": None})
    before = arena.meter.work

    first = arena.lookup(root, "bb")
    second = arena.lookup(root, "missing")

    assert arena.nodes[first].value is False
    assert second == -1
    assert arena.meter.work - before == 38
