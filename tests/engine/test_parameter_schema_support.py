import gc
import sys
from dataclasses import replace
from traceback import format_exception, walk_tb
from typing import cast

import pytest

from kora.engine import parameter_schema_support
from kora.engine.parameter_schema_references import ParameterReferenceBudget
from kora.engine.parameter_schema_support import (
    ParameterSchemaSupport,
    ParameterSupportFailure,
    ParameterSupportRejected,
    check_parameter_schema_support,
)
from kora.ir.json import JsonObject
from tests.engine.test_graph_types import custom
from tests.engine.test_parameter_schema_references import (
    PINNED_TYPE,
    PINNED_URI,
    Sources,
    ample,
    sources_for,
)

PRIVATE = "private-parameter-support-marker"
OTHER_TYPE = "example/Other"
OTHER_URI = "urn:kora:artifact:example:Other"


def check(
    sources: Sources,
    budget: ParameterReferenceBudget | None = None,
    *,
    frames: int = 100,
    depth: int = 100,
) -> ParameterSchemaSupport:
    return check_parameter_schema_support(
        sources.arena,
        budget=ample() if budget is None else budget,
        max_active_frames=frames,
        max_call_depth=depth,
    )


def test_parameter_schema_support_visits_all_callable_groups() -> None:
    sources = sources_for(
        {
            "propertyNames": True,
            "properties": {"z": True, "a": False},
            "oneOf": [False],
            "not": True,
            "items": False,
            "prefixItems": [True],
            "if": False,
            "then": True,
            "else": False,
            "dependentSchemas": {"x": True},
            "contains": False,
            "anyOf": [True],
            "allOf": [False],
            "additionalProperties": True,
        }
    )

    result = check(sources, frames=2, depth=2)

    assert result == ParameterSchemaSupport(reachable_positions=16, call_depth=2)


def test_parameter_schema_support_leaves_inert_locations_unvisited() -> None:
    sources = sources_for(
        {
            "$defs": {"unused": {"pattern": PRIVATE, "$ref": "#"}},
            "then": {"pattern": PRIVATE},
            "else": {"$ref": "#"},
            "const": {"pattern": PRIVATE},
            "dependencies": {"legacy": {"pattern": PRIVATE}},
            "contentSchema": {"pattern": PRIVATE},
        }
    )

    result = check(sources, frames=1, depth=1)

    assert result == ParameterSchemaSupport(reachable_positions=1, call_depth=1)


def test_parameter_schema_support_leaves_standalone_if_inert_until_referenced() -> None:
    body: JsonObject = {"pattern": PRIVATE}
    standalone = sources_for({"if": body})
    referenced = sources_for({"if": body, "$ref": "#/if"})
    armed = sources_for({"if": body, "else": True})

    result = check(standalone, frames=1, depth=1)
    with pytest.raises(ParameterSupportRejected) as reference:
        check(referenced)
    with pytest.raises(ParameterSupportRejected) as adjacent:
        check(armed)

    assert result == ParameterSchemaSupport(reachable_positions=1, call_depth=1)
    assert (
        reference.value.reason
        is adjacent.value.reason
        is ParameterSupportFailure.EXCLUDED
    )


@pytest.mark.parametrize(
    "keyword",
    ["pattern", "patternProperties", "unevaluatedItems", "unevaluatedProperties"],
)
def test_parameter_schema_support_checks_local_exclusions_before_references(
    keyword: str,
) -> None:
    sources = sources_for(
        {
            keyword: PRIVATE
            if keyword == "pattern"
            else {}
            if keyword == "patternProperties"
            else False,
            "$ref": "#/$defs/target",
            "$defs": {"target": True},
        }
    )
    budget = ParameterReferenceBudget(
        work=10_000, scratch_bytes=0, scratch_entries=10_000
    )

    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources, budget)

    assert refusal.value.reason is ParameterSupportFailure.EXCLUDED
    assert budget.scratch_bytes == 0


def test_parameter_schema_support_refuses_nested_resource_on_entry() -> None:
    sources = sources_for(
        {"allOf": [{"$id": "urn:example:nested", "pattern": PRIVATE}]}
    )

    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources)

    assert refusal.value.reason is ParameterSupportFailure.INVALID


@pytest.mark.parametrize(
    "schema",
    [
        {
            "$dynamicRef": "#/$defs/blocked",
            "$ref": "#/$defs/missing",
            "$defs": {"blocked": {"pattern": PRIVATE}},
        },
        {"allOf": [{"pattern": PRIVATE}, 7]},
        {"prefixItems": [{"pattern": PRIVATE}], "items": 7},
        {"properties": {"z": 7, "a": {"pattern": PRIVATE}}},
        {"if": {"pattern": PRIVATE}, "then": True, "else": 7},
    ],
    ids=[
        "reference-heads",
        "array-order",
        "prefix-before-tail",
        "named-order",
        "conditional-test",
    ],
)
def test_parameter_schema_support_stops_before_a_later_malformed_edge(
    schema: JsonObject,
) -> None:
    sources = sources_for(schema)

    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources)

    assert refusal.value.reason is ParameterSupportFailure.EXCLUDED


def test_parameter_schema_support_keeps_the_type_root_through_containment() -> None:
    sources = sources_for(
        {"$ref": PINNED_URI + "#", "$defs": {"leaf": {"pattern": PRIVATE}}},
        {
            PINNED_TYPE: custom(
                {
                    "$defs": {"leaf": True},
                    "properties": {"nested": {"$ref": "#/$defs/leaf"}},
                }
            )
        },
    )

    result = check(sources, frames=4, depth=4)

    assert result == ParameterSchemaSupport(reachable_positions=4, call_depth=4)


def test_parameter_schema_support_reuses_the_deep_suffix_of_a_completed_target() -> (
    None
):
    sources = sources_for(
        {
            "$defs": {"shared": {"properties": {"leaf": True}}},
            "allOf": [
                {"$ref": "#/$defs/shared"},
                {"not": {"not": {"$ref": "#/$defs/shared"}}},
            ],
        }
    )

    result = check(sources, frames=4, depth=6)
    with pytest.raises(ParameterSupportRejected) as depth:
        check(sources, frames=4, depth=5)
    with pytest.raises(ParameterSupportRejected) as frames:
        check(sources, frames=3, depth=6)

    assert result == ParameterSchemaSupport(reachable_positions=7, call_depth=6)
    assert depth.value.reason is frames.value.reason is ParameterSupportFailure.CAPACITY


@pytest.mark.parametrize(
    ("schema", "max_depth"),
    [
        ({"allOf": [{"not": True}, {"pattern": PRIVATE}]}, 2),
        ({"not": {"allOf": [{"not": True}, {"pattern": PRIVATE}]}}, 3),
        (
            {
                "$defs": {"leaf": True},
                "allOf": [
                    {"$ref": "#/$defs/leaf"},
                    {"not": {"$ref": "#/$defs/leaf", "not": {"pattern": PRIVATE}}},
                ],
            },
            3,
        ),
    ],
    ids=["root", "active-ancestor", "cached-suffix"],
)
def test_parameter_schema_support_stops_after_a_proved_depth_overflow(
    schema: JsonObject, max_depth: int
) -> None:
    sources = sources_for(schema)

    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources, depth=max_depth)

    assert refusal.value.reason is ParameterSupportFailure.CAPACITY


@pytest.mark.parametrize("cross_type", [False, True], ids=["local", "pinned-types"])
def test_parameter_schema_support_refuses_a_reachable_cycle(cross_type: bool) -> None:
    sources = (
        sources_for(
            {"$ref": PINNED_URI + "#"},
            {
                PINNED_TYPE: custom({"$ref": OTHER_URI + "#"}),
                OTHER_TYPE: custom({"$dynamicRef": PINNED_URI + "#"}),
            },
        )
        if cross_type
        else sources_for({"$ref": "#"})
    )

    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources)

    assert refusal.value.reason is ParameterSupportFailure.CYCLE


def test_parameter_schema_support_admits_exact_state_and_work() -> None:
    sources = sources_for({})
    budget = ParameterReferenceBudget(47, 0, 15)

    result = check(sources, budget, frames=1, depth=1)

    assert result == ParameterSchemaSupport(reachable_positions=1, call_depth=1)
    assert budget == ParameterReferenceBudget(0, 0, 0)


@pytest.mark.parametrize(
    ("work", "entries", "frames", "depth"),
    [(46, 15, 1, 1), (47, 14, 1, 1), (47, 15, 0, 1), (47, 15, 1, 0)],
    ids=["work", "entries", "frames", "depth"],
)
def test_parameter_schema_support_refuses_independently_short_limits(
    work: int, entries: int, frames: int, depth: int
) -> None:
    sources = sources_for({})
    budget = ParameterReferenceBudget(work, 0, entries)

    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources, budget, frames=frames, depth=depth)

    assert refusal.value.reason is ParameterSupportFailure.CAPACITY


def test_parameter_schema_support_admits_the_outgoing_containment_position() -> None:
    sources = sources_for({"not": True})
    budget = ParameterReferenceBudget(1000, 0, 30)
    short = ParameterReferenceBudget(1000, 0, 29)

    result = check(sources, budget, frames=2, depth=2)
    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources, short, frames=2, depth=2)

    assert result == ParameterSchemaSupport(reachable_positions=2, call_depth=2)
    assert budget.scratch_entries == 0
    assert refusal.value.reason is ParameterSupportFailure.CAPACITY


def test_parameter_schema_support_charges_both_references_to_one_target() -> None:
    sources = sources_for(
        {"$defs": {"leaf": True}, "$dynamicRef": "#/$defs/leaf", "$ref": "#/$defs/leaf"}
    )
    budget = ParameterReferenceBudget(100_000, 616, 271)
    short_bytes = ParameterReferenceBudget(100_000, 615, 271)
    short_entries = ParameterReferenceBudget(100_000, 616, 270)

    result = check(sources, budget)
    with pytest.raises(ParameterSupportRejected) as scratch:
        check(sources, short_bytes)
    with pytest.raises(ParameterSupportRejected) as entries:
        check(sources, short_entries)

    assert result == ParameterSchemaSupport(reachable_positions=2, call_depth=2)
    assert budget.scratch_bytes == 0
    assert budget.scratch_entries == 0
    assert (
        scratch.value.reason is entries.value.reason is ParameterSupportFailure.CAPACITY
    )


@pytest.mark.parametrize(
    ("schema", "scratch_bytes", "reason"),
    [
        (
            {"allOf": [True, {"pattern": PRIVATE}]},
            None,
            ParameterSupportFailure.EXCLUDED,
        ),
        (
            {"allOf": [True, {"$ref": "#/$defs/" + PRIVATE}]},
            None,
            ParameterSupportFailure.INVALID,
        ),
        (
            {"$defs": {PRIVATE: True}, "allOf": [True, {"$ref": "#/$defs/" + PRIVATE}]},
            0,
            ParameterSupportFailure.CAPACITY,
        ),
    ],
    ids=["excluded", "invalid-reference", "resolver-capacity"],
)
def test_parameter_schema_support_refusal_retains_no_private_traversal(
    schema: JsonObject, scratch_bytes: int | None, reason: ParameterSupportFailure
) -> None:
    sources = sources_for(schema)
    budget = (
        ample()
        if scratch_bytes is None
        else replace(ample(), scratch_bytes=scratch_bytes)
    )
    owner_type = cast("type[object]", vars(parameter_schema_support)["_Traversal"])
    collecting = gc.isenabled()

    gc.disable()
    try:
        try:
            raise RuntimeError(PRIVATE)
        except RuntimeError:
            with pytest.raises(ParameterSupportRejected) as refusal:
                check(sources, budget)
        retained = any(
            type(candidate) is owner_type
            and object.__getattribute__(candidate, "arena") is sources.arena
            for candidate in gc.get_objects()
        )
    finally:
        if collecting:
            gc.enable()
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert refusal.value.reason is reason
    assert refusal.value.__context__ is refusal.value.__cause__ is None
    assert frames["check_parameter_schema_support"] == {"reason": reason}
    assert {"_check", "walk", "_edges", "resolve_parameter_reference"}.isdisjoint(
        frames
    )
    assert PRIVATE not in "".join(format_exception(refusal.value))
    assert not retained


@pytest.mark.parametrize("frames", [True, -1, sys.maxsize + 1])
def test_parameter_schema_support_refuses_an_invalid_limit_domain(frames: int) -> None:
    sources = sources_for({})
    budget = ParameterReferenceBudget(100, 0, 100)

    with pytest.raises(ParameterSupportRejected) as refusal:
        check(sources, budget, frames=frames)

    assert refusal.value.reason is ParameterSupportFailure.INVALID
    assert budget == ParameterReferenceBudget(100, 0, 100)
