import sys
from dataclasses import dataclass
from traceback import format_exception, walk_tb
from typing import cast

import pytest

from kora.db.graph_value_json import ObjectReader
from kora.engine.graph_parameters import (
    build_graph_parameter_binding,
    read_graph_parameter_binding,
)
from kora.engine.graph_types import (
    build_graph_type_contracts,
    read_graph_type_contracts,
)
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceFailure,
    ParameterReferenceKeyword,
    ParameterReferenceRejected,
    ParameterSchemaArena,
    ParameterSchemaPosition,
    resolve_parameter_reference,
    schema_member,
)
from kora.engine.parameter_schema_source import build_parameter_schema_arena
from kora.ir import GraphDocument, render_ir_json
from kora.ir.json import JsonObject
from tests.engine.test_graph_types import custom, section_value
from tests.ir.document_cases import document

ENCODED_LIMIT = 100_000
PINNED_TYPE = "example/Pinned"
PINNED_URI = "urn:kora:artifact:example:Pinned"
PRIVATE = "private-reference-marker"


@dataclass(frozen=True, slots=True)
class Sources:
    arena: ParameterSchemaArena
    origin: int
    alternative: int | None = None

    def resolve(
        self,
        keyword: ParameterReferenceKeyword,
        budget: ParameterReferenceBudget,
        *,
        position: ParameterSchemaPosition | None = None,
    ) -> ParameterSchemaPosition:
        return resolve_parameter_reference(
            self.arena,
            ParameterSchemaPosition(self.arena.root, self.origin)
            if position is None
            else position,
            keyword=keyword,
            budget=budget,
        )


def sources_for(
    schema: JsonObject,
    contracts: JsonObject | None = None,
    *,
    nested: bool = False,
    selection_budget: ParameterReferenceBudget | None = None,
    materialization_budget: ParameterReferenceBudget | None = None,
) -> Sources:
    raw = document()
    cast("JsonObject", raw["graph"])["params"] = schema
    raw["type_contracts"] = {} if contracts is None else contracts
    pinned = GraphDocument.model_validate(raw)
    ir_text = render_ir_json(pinned)
    parameter_text = build_graph_parameter_binding(
        pinned, max_encoded_bytes=ENCODED_LIMIT
    )
    parameter_value = ObjectReader(parameter_text).object()
    read_graph_parameter_binding(
        pinned, parameter_value, max_encoded_bytes=ENCODED_LIMIT
    )
    type_text = build_graph_type_contracts(ir_text, max_encoded_bytes=ENCODED_LIMIT)
    records = section_value(type_text)
    read_graph_type_contracts(ir_text, records, max_encoded_bytes=ENCODED_LIMIT)
    arena = build_parameter_schema_arena(
        parameter_value["schema"],
        records,
        budget=ample() if materialization_budget is None else materialization_budget,
        selection_budget=selection_budget,
        max_container_depth=64,
        max_integer_bits=20_000,
    )
    if not nested:
        return Sources(arena, arena.root)
    lookup = ample()
    properties = schema_member(
        arena.nodes, arena.nodes[arena.root], "properties", lookup
    )
    assert properties is not None
    origin = schema_member(arena.nodes, arena.nodes[properties], "local", lookup)
    alternative = schema_member(arena.nodes, arena.nodes[properties], "other", lookup)
    assert origin is not None
    assert alternative is not None
    return Sources(arena, origin, alternative)


def ample() -> ParameterReferenceBudget:
    return ParameterReferenceBudget(ENCODED_LIMIT, 4 * ENCODED_LIMIT, ENCODED_LIMIT)


def test_parameter_references_preserve_the_original_root_after_a_cross_type_hop() -> (
    None
):
    sources = sources_for(
        {
            "$id": "urn:example:parameter-root",
            "$defs": {"leaf": False, "escaped/é~": True},
            "properties": {
                "local": {"$ref": "#/$defs/escaped~1%C3%A9~0"},
                "other": {
                    "$ref": "#/$defs/leaf",
                    "$dynamicRef": PINNED_URI + "#/$defs/leaf",
                },
            },
        },
        {
            PINNED_TYPE: custom(
                {
                    "$defs": {
                        "leaf": {
                            "$dynamicAnchor": "anchored",
                            "$ref": "#/$defs/escaped~1%C3%A9~0",
                        },
                        "escaped/é~": False,
                    }
                }
            )
        },
        nested=True,
    )
    alternative = sources.alternative
    assert alternative is not None
    budget = ample()

    local = sources.resolve(ParameterReferenceKeyword.REF, budget)
    local_at_shared_site = sources.resolve(
        ParameterReferenceKeyword.REF,
        budget,
        position=ParameterSchemaPosition(sources.arena.root, alternative),
    )
    external = sources.resolve(
        ParameterReferenceKeyword.DYNAMIC_REF,
        budget,
        position=ParameterSchemaPosition(sources.arena.root, alternative),
    )
    nested = sources.resolve(ParameterReferenceKeyword.REF, budget, position=external)
    roots = sources.arena.nodes[sources.arena.type_roots].members

    assert local.document_root == sources.arena.root
    assert sources.arena.nodes[local.node].value is True
    assert local_at_shared_site.document_root == sources.arena.root
    assert sources.arena.nodes[local_at_shared_site.node].value is False
    assert external.document_root == nested.document_root == roots[0][1]
    assert external.document_root != local.document_root
    assert external.node != external.document_root
    assert nested.node != local.node
    assert sources.arena.nodes[nested.node].value is False


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("#/prefixItems/0", False),
        ("#/$defs/$id", True),
        ("#/dependencies/boolean", False),
    ],
    ids=["typed-array-index", "member-named-id", "inert-typed-target"],
)
def test_parameter_references_resolve_typed_boolean_locations(
    reference: str, expected: bool
) -> None:
    sources = sources_for(
        {
            "$ref": reference,
            "$defs": {"$id": True},
            "prefixItems": [False],
            "dependencies": {"boolean": False, "literal": ["data"]},
        }
    )

    target = sources.resolve(ParameterReferenceKeyword.REF, ample())

    assert target.document_root == sources.arena.root
    assert sources.arena.nodes[target.node].value is expected


@pytest.mark.parametrize(
    "reference",
    ["", "#", PINNED_URI, PINNED_URI + "#"],
    ids=["empty", "hash", "named", "named-hash"],
)
def test_parameter_references_resolve_empty_fragments_at_the_selected_root(
    reference: str,
) -> None:
    sources = sources_for({"$ref": reference}, {PINNED_TYPE: custom({})})
    expected = (
        sources.arena.nodes[sources.arena.type_roots].members[0][1]
        if reference.startswith(PINNED_URI)
        else sources.arena.root
    )

    target = sources.resolve(ParameterReferenceKeyword.REF, ample())

    assert target == ParameterSchemaPosition(expected, expected)


@pytest.mark.parametrize(
    "reference",
    [
        "#/const/data",
        "#/dependencies/literal/0",
        "#/prefixItems/01",
        "#/prefixItems/" + "9" * 5000,
        "#/$defs/resource/properties/child",
        "#/$defs/bad~2key",
        "#anchor",
        "https://example.invalid/" + PRIVATE,
        PINNED_URI + "#",
    ],
    ids=[
        "literal-object",
        "literal-legacy-array",
        "leading-zero",
        "oversized-index",
        "nested-resource-ancestor",
        "invalid-tilde",
        "anchor",
        "external",
        "authored-id-is-not-a-pinned-name",
    ],
)
def test_parameter_references_refuse_outside_the_closed_typed_target_set(
    reference: str,
) -> None:
    sources = sources_for(
        {
            "$id": PINNED_URI,
            "$ref": reference,
            "$defs": {
                "resource": {
                    "$id": "urn:example:disallowed-resource",
                    "properties": {"child": True},
                }
            },
            "const": {"data": {}},
            "dependencies": {"literal": ["data"]},
            "prefixItems": [False],
        }
    )

    with pytest.raises(ParameterReferenceRejected) as refusal:
        sources.resolve(ParameterReferenceKeyword.REF, ample())

    assert refusal.value.reason is ParameterReferenceFailure.INVALID
    assert PRIVATE not in "".join(format_exception(refusal.value))


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/properties/0", "properties": [False]},
        {"$ref": "#/prefixItems/slot", "prefixItems": {"slot": False}},
    ],
    ids=["list-in-named-position", "object-in-listed-position"],
)
def test_parameter_references_refuse_a_malformed_intermediate_position(
    schema: JsonObject,
) -> None:
    sources = sources_for(schema)

    with pytest.raises(ParameterReferenceRejected) as refusal:
        sources.resolve(ParameterReferenceKeyword.REF, ample())

    assert refusal.value.reason is ParameterReferenceFailure.INVALID


def test_parameter_reference_budget_accepts_exact_cumulative_charges() -> None:
    sources = sources_for({"$ref": "#"})
    budget = ParameterReferenceBudget(62, 88, 36)
    expected = ParameterSchemaPosition(sources.arena.root, sources.arena.root)

    first = sources.resolve(ParameterReferenceKeyword.REF, budget)
    second = sources.resolve(ParameterReferenceKeyword.REF, budget)

    assert first == second == expected
    assert budget == ParameterReferenceBudget(0, 0, 0)


@pytest.mark.parametrize(
    ("work_short", "bytes_short", "entries_short"),
    [(1, 0, 0), (0, 1, 0), (0, 0, 1)],
    ids=["work", "scratch-bytes", "scratch-entries"],
)
def test_parameter_reference_budget_refuses_an_independently_short_allowance(
    work_short: int, bytes_short: int, entries_short: int
) -> None:
    sources = sources_for({"$ref": "#"})
    budget = ParameterReferenceBudget(
        62 - work_short, 88 - bytes_short, 36 - entries_short
    )
    expected = ParameterSchemaPosition(sources.arena.root, sources.arena.root)

    first = sources.resolve(ParameterReferenceKeyword.REF, budget)
    with pytest.raises(ParameterReferenceRejected) as refusal:
        sources.resolve(ParameterReferenceKeyword.REF, budget)

    assert first == expected
    assert refusal.value.reason is ParameterReferenceFailure.CAPACITY
    if bytes_short or entries_short:
        assert budget == ParameterReferenceBudget(
            11, 4 - bytes_short, 2 - entries_short
        )


def test_parameter_reference_failure_discards_source_and_ambient_exception() -> None:
    sources = sources_for({"$ref": "#/$defs/" + PRIVATE})

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterReferenceRejected) as refusal:
            sources.resolve(ParameterReferenceKeyword.REF, ample())
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert refusal.value.reason is ParameterReferenceFailure.INVALID
    assert refusal.value.__context__ is refusal.value.__cause__ is None
    assert frames["resolve_parameter_reference"] == {
        "reason": ParameterReferenceFailure.INVALID
    }
    assert {"_descend", "_resolve", "decode_reference"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(refusal.value))


@pytest.mark.parametrize("work", [True, -1, sys.maxsize + 1])
def test_parameter_references_refuse_invalid_budget_inputs(work: int) -> None:
    sources = sources_for({"$ref": "#"})
    budget = ParameterReferenceBudget(work, 1000, 1000)

    with pytest.raises(ParameterReferenceRejected) as refusal:
        sources.resolve(ParameterReferenceKeyword.REF, budget)

    assert refusal.value.reason is ParameterReferenceFailure.INVALID
    assert budget == ParameterReferenceBudget(work, 1000, 1000)
