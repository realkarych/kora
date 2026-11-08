import gc
import math
from traceback import format_exception, walk_tb
from typing import cast

import pytest

from kora.engine import parameter_schema_source
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterSchemaArena,
    schema_member,
)
from kora.engine.parameter_schema_source import (
    ParameterSchemaSourceFailure,
    ParameterSchemaSourceRejected,
    build_parameter_schema_arena,
)
from kora.engine.parameter_schema_support import (
    ParameterSchemaSupport,
    check_parameter_schema_support,
)
from kora.ir.json import JsonObject
from tests.engine.test_parameter_schema_references import ample, sources_for

PRIVATE = "private-parameter-schema-source"


def member(arena: ParameterSchemaArena, node: int, name: str) -> int:
    result = schema_member(arena.nodes, arena.nodes[node], name, ample())
    assert result is not None
    return result


def test_schema_source_preserves_canonical_scalars_in_ordered_containers() -> None:
    sources = sources_for(
        {
            "$ref": "#/$defs/leaf",
            "const": {
                "😀": "astral",
                "\ud800": [2**80, -0.0, "\ud800"],
                "é": "accent",
                "a": "first",
            },
            "$defs": {"leaf": True},
        }
    )
    arena = sources.arena

    outcome = check_parameter_schema_support(
        arena, budget=ample(), max_active_frames=2, max_call_depth=2
    )
    const = arena.nodes[member(arena, arena.root, "const")]
    names = [arena.nodes[key].value for key, _ in const.members]
    array = arena.nodes[const.members[2][1]]
    integer, floating, string = (arena.nodes[child] for child in array.children)

    assert outcome == ParameterSchemaSupport(reachable_positions=2, call_depth=2)
    assert names == ["a", "é", "\ud800", "😀"]
    assert const.maximum_key == 1
    assert integer.value == 2**80
    assert integer.spelling == "1208925819614629174706176"
    assert type(floating.value) is float
    assert math.copysign(1, floating.value) == -1
    assert floating.spelling == "-0.0"
    assert string.value == "\ud800"
    assert string.size == 8


def test_schema_source_named_table_does_not_add_document_depth() -> None:
    parameters: JsonObject = {}
    types: list[JsonObject] = [
        {"name": "example/A", "schema": {}},
        {"name": "example/Z", "schema": {"type": "integer"}},
    ]

    arena = build_parameter_schema_arena(
        parameters, types, budget=ample(), max_container_depth=1, max_integer_bits=0
    )
    table = arena.nodes[arena.type_roots]
    named = member(arena, arena.type_roots, "example/Z")
    type_node = member(arena, named, "type")

    assert [arena.nodes[key].value for key, _ in table.members] == [
        "example/A",
        "example/Z",
    ]
    assert arena.nodes[type_node].value == "integer"
    assert named != arena.root


def test_schema_source_duplicates_acyclic_occurrences_and_detaches() -> None:
    shared: JsonObject = {"enabled": True}
    parameters: JsonObject = {"right": shared, "left": shared}

    arena = build_parameter_schema_arena(
        parameters, [], budget=ample(), max_container_depth=2, max_integer_bits=0
    )
    shared["enabled"] = False
    left = member(arena, arena.root, "left")
    right = member(arena, arena.root, "right")
    left_leaf = member(arena, left, "enabled")
    right_leaf = member(arena, right, "enabled")

    assert left != right
    assert left_leaf != right_leaf
    assert arena.nodes[left_leaf].value is arena.nodes[right_leaf].value is True


def metered_type_records() -> list[JsonObject]:
    return [{"name": "A", "schema": {}}, {"name": "Z", "schema": {}}]


def test_schema_source_accepts_exact_work_payload_and_entries() -> None:
    parameters: JsonObject = {"x": [True]}
    records = metered_type_records()
    budget = ParameterReferenceBudget(94, 24, 126)

    arena = build_parameter_schema_arena(
        parameters, records, budget=budget, max_container_depth=2, max_integer_bits=0
    )

    assert len(arena.nodes) == 9
    assert budget == ParameterReferenceBudget(0, 0, 0)


@pytest.mark.parametrize(
    ("work", "payload", "entries", "depth"),
    [(93, 24, 126, 2), (94, 23, 126, 2), (94, 24, 125, 2), (94, 24, 126, 1)],
    ids=["work", "payload", "entries", "depth"],
)
def test_schema_source_refuses_an_independently_short_bound(
    work: int, payload: int, entries: int, depth: int
) -> None:
    parameters: JsonObject = {"x": [True]}
    records = metered_type_records()
    budget = ParameterReferenceBudget(work, payload, entries)

    with pytest.raises(ParameterSchemaSourceRejected) as refusal:
        build_parameter_schema_arena(
            parameters,
            records,
            budget=budget,
            max_container_depth=depth,
            max_integer_bits=0,
        )

    assert refusal.value.reason is ParameterSchemaSourceFailure.CAPACITY


def test_schema_source_keeps_integer_width_separate() -> None:
    parameters: JsonObject = {"a": [PRIVATE], "minimum": 2**80}
    limited = ample()
    builder = cast("type[object]", vars(parameter_schema_source)["_Builder"])
    collecting = gc.isenabled()

    arena = build_parameter_schema_arena(
        parameters, [], budget=ample(), max_container_depth=2, max_integer_bits=81
    )
    gc.disable()
    try:
        with pytest.raises(ParameterSchemaSourceRejected) as refusal:
            build_parameter_schema_arena(
                parameters,
                [],
                budget=limited,
                max_container_depth=2,
                max_integer_bits=80,
            )
        retained = any(
            type(candidate) is builder
            and object.__getattribute__(candidate, "budget") is limited
            for candidate in gc.get_objects()
        )
    finally:
        if collecting:
            gc.enable()

    assert arena.nodes[member(arena, arena.root, "minimum")].value == 2**80
    assert refusal.value.reason is ParameterSchemaSourceFailure.CAPACITY
    assert not retained


INVALID_SOURCES: list[tuple[object, object]] = [
    (False, []),
    ({}, {}),
    ({7: True}, []),
    ({}, [{"name": "example/Type", "schema": False}]),
    ({}, [{"name": "Z", "schema": {}}, {"name": "A", "schema": {}}]),
    ({}, [{"name": "A", "schema": {}}, {"name": "A", "schema": {}}]),
    ({}, [{"name": "A", "schema": {}, **{str(i): None for i in range(7)}}]),
    ({}, [{"name": "A", "schema": {}, 7: None}]),
]


@pytest.mark.parametrize(
    ("parameters", "types"),
    INVALID_SOURCES,
    ids=[
        "parameter-root",
        "type-list",
        "object-key",
        "named-root",
        "unsorted-name",
        "duplicate-name",
        "wide-record",
        "record-key",
    ],
)
def test_schema_source_refuses_invalid_input_shape(
    parameters: object, types: object
) -> None:
    budget = ample()

    with pytest.raises(ParameterSchemaSourceRejected) as refusal:
        build_parameter_schema_arena(
            parameters,
            types,
            budget=budget,
            max_container_depth=20,
            max_integer_bits=100,
        )

    assert refusal.value.reason is ParameterSchemaSourceFailure.INVALID


def test_schema_source_refuses_late_cycle_without_retaining_private_state() -> None:
    parameters: JsonObject = {"a": [True], "z": PRIVATE}
    parameters["zz"] = parameters

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterSchemaSourceRejected) as refusal:
            build_parameter_schema_arena(
                parameters,
                [],
                budget=ample(),
                max_container_depth=20,
                max_integer_bits=0,
            )
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert refusal.value.reason is ParameterSchemaSourceFailure.INVALID
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert frames["build_parameter_schema_arena"] == {
        "reason": ParameterSchemaSourceFailure.INVALID
    }
    assert {"_build", "_open", "document", "named_table"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(refusal.value))
