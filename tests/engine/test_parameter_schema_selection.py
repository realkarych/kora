import gc
from traceback import format_exception, walk_tb
from typing import cast

import pytest

from kora.engine import parameter_schema_source
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceKeyword,
    ParameterSchemaArena,
    ParameterSchemaPosition,
)
from kora.engine.parameter_schema_source import (
    ParameterSchemaSourceFailure,
    ParameterSchemaSourceRejected,
)
from kora.engine.parameter_schema_support import (
    ParameterSchemaSupport,
    check_parameter_schema_support,
)
from kora.ir.json import JsonObject
from tests.engine.test_graph_types import custom
from tests.engine.test_parameter_schema_references import ample, sources_for
from tests.engine.test_parameter_schema_source import member

FIRST = "urn:kora:artifact:example:First"
SECOND = "urn:kora:artifact:example:Second"
UNRELATED = "urn:kora:artifact:example:Unrelated"
METERED_ROOT: JsonObject = {"$ref": FIRST + "#/$defs/leaf"}
METERED_TYPES: JsonObject = {"example/First": custom({"$defs": {"leaf": True}})}


def named_roots(arena: ParameterSchemaArena) -> list[object]:
    return [arena.nodes[key].value for key, _ in arena.nodes[arena.type_roots].members]


def test_selection_closes_typed_inert_references_without_executing_them() -> None:
    root: JsonObject = {
        "$ref": "#/$defs/leaf",
        "$defs": {
            "leaf": True,
            "compat": {"$recursiveRef": FIRST + "#/$defs/alias"},
        },
        "dependencies": {
            "branch": {"$ref": SECOND},
            "names": [UNRELATED],
        },
        "const": {"$ref": UNRELATED},
    }
    duplicate = custom({"$defs": {"same": True}})
    contracts: JsonObject = {
        "example/First": custom(
            {
                "$defs": {
                    "alias": {"$ref": "#/$defs/leaf"},
                    "cycle": {"$ref": SECOND + "#/$defs/cycle"},
                    "leaf": False,
                    "third": {"$ref": "urn:kora:artifact:example:Third"},
                }
            },
            parent="example/Parent",
        ),
        "example/Parent": custom({}),
        "example/Second": custom(
            {
                "$defs": {
                    "cycle": {"$ref": FIRST + "#/$defs/cycle"},
                    "twin": {"$ref": "urn:kora:artifact:example:Twin"},
                }
            }
        ),
        "example/Third": duplicate,
        "example/Twin": duplicate,
        "example/Unrelated": custom({}),
    }

    sources = sources_for(root, contracts, selection_budget=ample())
    arena = sources.arena
    defs = member(arena, arena.root, "$defs")
    compat = member(arena, defs, "compat")
    selected = sources.resolve(
        ParameterReferenceKeyword.RECURSIVE_REF,
        ample(),
        position=ParameterSchemaPosition(arena.root, compat),
    )
    target = sources.resolve(ParameterReferenceKeyword.REF, ample(), position=selected)
    support = check_parameter_schema_support(
        arena, budget=ample(), max_active_frames=2, max_call_depth=2
    )

    assert named_roots(arena) == [
        "example/First",
        "example/Second",
        "example/Third",
        "example/Twin",
    ]
    assert member(arena, arena.type_roots, "example/Third") != member(
        arena, arena.type_roots, "example/Twin"
    )
    assert target.document_root == selected.document_root != arena.root
    assert arena.nodes[target.node].value is False
    assert support == ParameterSchemaSupport(reachable_positions=2, call_depth=2)


def test_unselected_body_growth_consumes_neither_parameter_budget() -> None:
    short_types: JsonObject = {**METERED_TYPES, "example/Unrelated": custom({})}
    wide_types: JsonObject = {
        **METERED_TYPES,
        "example/Unrelated": custom({"maximum": 1 << 1000}),
    }
    short_selection, wide_selection = ample(), ample()
    short_materialization, wide_materialization = ample(), ample()

    short = sources_for(
        METERED_ROOT,
        short_types,
        selection_budget=short_selection,
        materialization_budget=short_materialization,
    )
    wide = sources_for(
        METERED_ROOT,
        wide_types,
        selection_budget=wide_selection,
        materialization_budget=wide_materialization,
    )

    assert short.arena == wide.arena
    assert short_selection == wide_selection
    assert short_materialization == wide_materialization


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/const", "const": {}},
        {"$defs": {"latent": {"$dynamicRef": "https://private.invalid"}}},
        {"$defs": {"latent": {"$recursiveRef": FIRST + "#/$defs/missing"}}},
        {"$defs": {"latent": {"$id": "urn:private:subschema"}}},
        {"allOf": [{"$ref": False}]},
        {"$ref": UNRELATED},
    ],
    ids=[
        "data-target",
        "external",
        "absent-target",
        "nested-id",
        "scalar",
        "absent-type",
    ],
)
def test_selection_refuses_unbound_or_invalid_typed_references(
    schema: JsonObject,
) -> None:
    budget = ample()

    with pytest.raises(ParameterSchemaSourceRejected) as refusal:
        sources_for(schema, METERED_TYPES, selection_budget=budget)

    assert refusal.value.reason is ParameterSchemaSourceFailure.INVALID


def test_late_selected_pointer_refusal_releases_private_builder_and_arena() -> None:
    private = "private-missing-target"
    schema: JsonObject = {"$defs": {"latent": {"$ref": FIRST + "#/$defs/" + private}}}
    builder = cast("type[object]", vars(parameter_schema_source)["_Builder"])
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = {
            id(item)
            for item in gc.get_objects()
            if type(item) is builder or type(item) is ParameterSchemaArena
        }

        try:
            raise RuntimeError(private)
        except RuntimeError:
            with pytest.raises(ParameterSchemaSourceRejected) as refusal:
                sources_for(schema, METERED_TYPES, selection_budget=ample())
        after = {
            id(item)
            for item in gc.get_objects()
            if type(item) is builder or type(item) is ParameterSchemaArena
        }
    finally:
        if was_enabled:
            gc.enable()
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert after == before
    assert refusal.value.reason is ParameterSchemaSourceFailure.INVALID
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert frames["build_parameter_schema_arena"] == {
        "reason": ParameterSchemaSourceFailure.INVALID
    }
    assert private not in "".join(format_exception(refusal.value))


def test_selection_and_materialization_accept_their_exact_limits() -> None:
    selection = ParameterReferenceBudget(964, 2105, 824)
    materialization = ParameterReferenceBudget(248, 508, 177)

    sources = sources_for(
        METERED_ROOT,
        METERED_TYPES,
        selection_budget=selection,
        materialization_budget=materialization,
    )

    assert named_roots(sources.arena) == ["example/First"]
    assert selection == materialization == ParameterReferenceBudget(0, 0, 0)


@pytest.mark.parametrize(
    ("selection_limits", "materialization_limits"),
    [
        ((963, 2105, 824), (248, 508, 177)),
        ((964, 2104, 824), (248, 508, 177)),
        ((964, 2105, 823), (248, 508, 177)),
        ((964, 2105, 824), (247, 508, 177)),
        ((964, 2105, 824), (248, 507, 177)),
        ((964, 2105, 824), (248, 508, 176)),
    ],
    ids=[
        "selection-work",
        "selection-payload",
        "selection-entries",
        "materialization-work",
        "materialization-payload",
        "materialization-entries",
    ],
)
def test_selection_and_materialization_refuse_independent_short_limits(
    selection_limits: tuple[int, int, int],
    materialization_limits: tuple[int, int, int],
) -> None:
    selection = ParameterReferenceBudget(*selection_limits)
    materialization = ParameterReferenceBudget(*materialization_limits)

    with pytest.raises(ParameterSchemaSourceRejected) as refusal:
        sources_for(
            METERED_ROOT,
            METERED_TYPES,
            selection_budget=selection,
            materialization_budget=materialization,
        )

    assert refusal.value.reason is ParameterSchemaSourceFailure.CAPACITY


def test_empty_selection_uses_no_named_index_or_schema_budget() -> None:
    selection = ParameterReferenceBudget(12, 0, 13)
    materialization = ParameterReferenceBudget(14, 0, 21)

    sources = sources_for(
        {},
        METERED_TYPES,
        selection_budget=selection,
        materialization_budget=materialization,
    )

    assert named_roots(sources.arena) == []
    assert selection == materialization == ParameterReferenceBudget(0, 0, 0)


@pytest.mark.parametrize(
    ("work", "entries"), [(11, 13), (12, 12)], ids=["work", "entries"]
)
def test_empty_selection_refuses_an_independently_short_bound(
    work: int, entries: int
) -> None:
    selection = ParameterReferenceBudget(work, 0, entries)
    materialization = ParameterReferenceBudget(14, 0, 21)

    with pytest.raises(ParameterSchemaSourceRejected) as refusal:
        sources_for(
            {},
            METERED_TYPES,
            selection_budget=selection,
            materialization_budget=materialization,
        )

    assert refusal.value.reason is ParameterSchemaSourceFailure.CAPACITY
