import gc
from copy import deepcopy
from dataclasses import replace
from types import CodeType, FunctionType, GeneratorType
from typing import Literal, Protocol, cast

import pytest
from jsonschema import Draft202012Validator

from kora.ir import (
    CustomTypeContract,
    structured_schema_adapter,
    structured_schema_equality,
    validate_type_schemas,
)
from kora.ir.json import JsonObject
from kora.ir.structured_output import StructuredOutput, checked_value
from kora.ir.structured_schema_adapter import decide_prepared_schema
from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    NativeCursor,
    NativeObjectIndex,
    arena_cursor,
    native_cursor,
)
from kora.ir.structured_schema_evaluation_policy import (
    PARAMETERS,
    POLICY,
    EvaluationRefused,
    StructuredSchemaEvaluationPolicy,
    bind_evaluation_policy,
)
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import PreparedStructuredSchemaInput
from kora.ir.structured_schema_numbers import DecimalNumber
from tests.ir.structured_schema_cases import (
    HostileInteger,
    arena_nodes,
    preparation_input,
    prepared,
    stored_field,
)
from tests.ir.test_structured_schema_evaluation_work import (
    Dimension,
    counters,
    remaining_meter,
)
from tests.ir.type_schema_cases import document_for

GUARDS: dict[str, object] = {
    "additionalProperties": False,
    "allOf": [True],
    "anyOf": [True],
    "contains": True,
    "dependentSchemas": {"x": True},
    "if": True,
    "items": True,
    "not": False,
    "oneOf": [True],
    "prefixItems": [True],
    "properties": {"x": True},
    "propertyNames": True,
}
PRIMITIVE_VECTORS: dict[str, tuple[object, object, object]] = {
    "const": ({"n": [1.0]}, {"n": [1]}, {"n": [True]}),
    "enum": ([False, {"n": 1.0}], {"n": 1}, {"n": True}),
    "exclusiveMaximum": (0.3, 0.2, 0.3),
    "exclusiveMinimum": (0.2, 0.3, 0.2),
    "maxItems": (1, [0], [0, 1]),
    "maxLength": (1, "😀", "e\u0301"),
    "maxProperties": (1, {"a": 1}, {"a": 1, "b": 2}),
    "maximum": (0, -0.0, 0.1),
    "minItems": (2, [None, False], [None]),
    "minLength": (2, "e\u0301", "😀"),
    "minProperties": (2, {"a": 1, "b": 2}, {"a": 1}),
    "minimum": (1e23, 10**23, int(1e23)),
    "multipleOf": (0.1, 0.3, 0.31),
}


def policy() -> StructuredSchemaEvaluationPolicy:
    return bind_evaluation_policy({"policy": POLICY, "parameters": dict(PARAMETERS)})


def admitted(schema: object) -> tuple[PreparedStructuredSchemaInput, int]:
    definitions = {"case": deepcopy(schema)}
    if type(schema) is dict:
        original = cast("dict[str, object]", schema)
        if "$defs" in original:
            definitions.update(deepcopy(cast("dict[str, object]", original["$defs"])))
    document = document_for(cast("JsonObject", {"$defs": definitions}))
    syntax = validate_type_schemas(document)
    assert syntax.schemas_valid
    contract = document.type_contracts["example/Value"]
    assert isinstance(contract, CustomTypeContract)
    holder = prepared(preparation_input(dict(contract.schema_)))
    nodes = arena_nodes(holder)
    positions = cast(
        "tuple[tuple[int, int, int, int, int], ...]", stored_field(holder, "_positions")
    )
    position = next(
        index
        for index, row in enumerate(positions)
        if row[0] == 1 and row[4] >= 0 and nodes[row[4]].value == "case"
    )
    return holder, position


def decide(
    holder: PreparedStructuredSchemaInput,
    position: int,
    value: object,
    meter: EvaluationMeter,
) -> bool:
    tree = checked_value(StructuredOutput(value))
    cursor = native_cursor(tree, meter)
    return decide_prepared_schema(holder, position, cursor, policy(), meter)


def private_function(name: str) -> FunctionType:
    return cast("FunctionType", vars(structured_schema_adapter)[name])


def traceback_codes(error: BaseException) -> list[CodeType]:
    result: list[CodeType] = []
    current = error.__traceback__
    while current is not None:
        result.append(current.tb_frame.f_code)
        current = current.tb_next
    return result


@pytest.mark.parametrize(
    ("schema", "value", "expected", "paid"),
    [
        (True, None, True, (171, 0, 19)),
        (False, None, False, (171, 0, 19)),
        ({}, None, True, (7006, 0, 91)),
        ({"type": "null"}, None, True, (10353, 0, 97)),
        ({"type": "boolean"}, None, False, (10342, 0, 98)),
        ({"else": False, "then": True}, None, True, (13578, 0, 101)),
        (
            {"$defs": {"t": {"type": "null"}}, "$ref": "#/$defs/t"},
            None,
            True,
            (34220, 0, 207),
        ),
        (
            {"$defs": {"t": False}, "$ref": "#/$defs/t"},
            None,
            False,
            (11250, 0, 98),
        ),
        ({"type": "integer"}, 1.0, True, (14534, 53, 100)),
        ({"required": ["a"]}, {"a": False}, True, (10563, 0, 105)),
    ],
)
def test_adapter_real_engine_matches_complete_decision_ledgers(
    schema: object, value: object, expected: bool, paid: tuple[int, int, int]
) -> None:
    holder, position = admitted(schema)
    original_nodes = arena_nodes(holder)
    meter = EvaluationMeter()

    result = decide(holder, position, value, meter)

    assert result is expected
    assert counters(meter) == paid
    assert arena_nodes(holder) is original_nodes


@pytest.mark.parametrize(
    ("schema", "value", "dimension", "capacity", "paid"),
    [
        (True, None, "work", 3, (3, 0, 1)),
        (True, None, "work", 5, (5, 0, 1)),
        (True, None, "work", 7, (7, 0, 1)),
        (True, None, "work", 9, (9, 0, 1)),
        (True, None, "work", 170, (170, 0, 19)),
        (True, None, "entries", 18, (151, 0, 18)),
        ({}, None, "entries", 54, (280, 0, 19)),
        ({}, None, "work", 388, (388, 0, 55)),
        ({"type": "null"}, None, "work", 2072, (1277, 0, 55)),
        ({"type": "null"}, None, "entries", 58, (1277, 0, 55)),
        ({"type": "null"}, None, "work", 2073, (2073, 0, 59)),
        ({"type": "null"}, None, "work", 393, (393, 0, 55)),
        ({"type": "null"}, None, "entries", 59, (2218, 0, 59)),
        ({"type": "null"}, None, "work", 10257, (10257, 0, 95)),
        ({"type": "null"}, None, "work", 10259, (10259, 0, 95)),
        ({"type": "null"}, None, "work", 10261, (10261, 0, 95)),
        ({"type": "boolean"}, None, "work", 10341, (10340, 0, 97)),
        ({"type": "boolean"}, None, "entries", 97, (10340, 0, 97)),
        ({"contains": True}, None, "work", 10427, (10427, 0, 96)),
        ({"contains": True}, None, "entries", 96, (10427, 0, 96)),
        ({"type": "null"}, None, "work", 10286, (10267, 0, 96)),
        ({"type": "null"}, None, "work", 10287, (10287, 0, 96)),
        ({}, None, "work", 7004, (7003, 0, 91)),
        ({}, None, "work", 7005, (7005, 0, 91)),
        ({"else": False, "then": True}, None, "work", 13572, (13554, 0, 101)),
        ({"type": "integer"}, 1.0, "bytes", 52, (14517, 51, 100)),
        ({"type": "integer"}, 1.0, "entries", 99, (14499, 50, 99)),
        ({"type": "integer"}, 1.0, "work", 14532, (14532, 53, 100)),
        ({"type": "integer"}, 1.0, "work", 14533, (14533, 53, 100)),
    ],
)
def test_adapter_atomic_fences_refuse_before_unpaid_actions(
    schema: object,
    value: object,
    dimension: Dimension,
    capacity: int,
    paid: tuple[int, int, int],
) -> None:
    holder, position = admitted(schema)
    meter = remaining_meter(dimension, capacity)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        decide(holder, position, value, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert (
        tuple(
            after - start for after, start in zip(counters(meter), before, strict=True)
        )
        == paid
    )
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None


@pytest.mark.parametrize(("capacity", "entered"), [(10286, False), (10287, True)])
def test_adapter_initial_ticket_precedes_real_first_handler_entry(
    capacity: int, entered: bool
) -> None:
    holder, position = admitted({"type": "null"})
    meter = remaining_meter("work", capacity)
    handler_code = private_function("_type").__code__

    with pytest.raises(EvaluationRefused) as rejected:
        decide(holder, position, None, meter)
    codes = traceback_codes(rejected.value)

    assert (handler_code in codes) is entered
    assert private_function("_drive").__code__ in codes
    assert arena_cursor.__code__ not in codes
    del codes, rejected


@pytest.mark.parametrize("keyword", PRIMITIVE_VECTORS)
def test_adapter_dispatches_bounded_primitive_assertions(keyword: str) -> None:
    argument, accepted, rejected = PRIMITIVE_VECTORS[keyword]
    holder, position = admitted({keyword: argument})

    matching = decide(holder, position, accepted, EvaluationMeter())
    mismatching = decide(holder, position, rejected, EvaluationMeter())

    assert matching is True
    assert mismatching is False


@pytest.mark.parametrize(
    "keyword", tuple(key for key in PRIMITIVE_VECTORS if key not in ("const", "enum"))
)
def test_adapter_primitive_assertions_preserve_inapplicable_kinds(keyword: str) -> None:
    argument, _, _ = PRIMITIVE_VECTORS[keyword]
    holder, position = admitted({keyword: argument})

    result = decide(holder, position, None, EvaluationMeter())

    assert result is True


@pytest.mark.parametrize(
    ("schema", "paid", "short_entry_paid"),
    [
        ({"minimum": 0}, (10411, 0, 97), (10395, 0, 96)),
        ({"enum": [None, False]}, (10378, 0, 101), (10331, 0, 99)),
        ({"enum": [False, None]}, (10432, 0, 105), (10385, 0, 103)),
    ],
)
@pytest.mark.parametrize("dimension", ["work", "entries"])
def test_adapter_primitive_leaf_uses_shared_meter(
    schema: object,
    paid: tuple[int, int, int],
    short_entry_paid: tuple[int, int, int],
    dimension: Literal["work", "entries"],
) -> None:
    holder, position = admitted(schema)
    capacity = paid[0 if dimension == "work" else 2]
    exact = remaining_meter(dimension, capacity)
    short = remaining_meter(dimension, capacity - 1)
    exact_before, short_before = counters(exact), counters(short)

    result = decide(holder, position, None, exact)
    with pytest.raises(EvaluationRefused) as exhausted:
        decide(holder, position, None, short)

    assert result is True
    assert exhausted.value.reason == "evaluation_exhausted"
    assert exhausted.value.__cause__ is None
    assert exhausted.value.__context__ is None
    assert (
        tuple(
            after - before
            for after, before in zip(counters(exact), exact_before, strict=True)
        )
        == paid
    )
    assert tuple(
        after - before
        for after, before in zip(counters(short), short_before, strict=True)
    ) == ((paid[0] - 1, paid[1], paid[2]) if dimension == "work" else short_entry_paid)


@pytest.mark.parametrize(
    ("schema", "accepted", "rejected"),
    [
        ({"required": ["a"]}, {"a": None}, {"b": None}),
        ({"dependentRequired": {"a": ["b"]}}, {"a": False, "b": 0}, {"a": False}),
        ({"dependentRequired": {"a": ["b"]}}, {}, {"a": None}),
        ({"uniqueItems": True}, [True, 1], [{"n": 1}, {"n": 1.0}]),
    ],
)
def test_adapter_dispatches_collection_assertions(
    schema: object, accepted: object, rejected: object
) -> None:
    holder, position = admitted(schema)

    matching = decide(holder, position, accepted, EvaluationMeter())
    mismatching = decide(holder, position, rejected, EvaluationMeter())

    assert matching is True
    assert mismatching is False


@pytest.mark.parametrize(
    ("schema", "value", "paid"),
    [
        ({"required": []}, {"a": False}, (10472, 0, 97)),
        ({"dependentRequired": {}}, {"a": False}, (10789, 0, 97)),
        ({"required": ["a"]}, None, (10438, 0, 96)),
    ],
)
def test_adapter_membership_skips_index_when_unneeded(
    schema: object, value: object, paid: tuple[int, int, int]
) -> None:
    holder, position = admitted(schema)
    exact = remaining_meter("work", paid[0])
    short = remaining_meter("work", paid[0] - 1)
    before = exact.work

    result = decide(holder, position, value, exact)
    with pytest.raises(EvaluationRefused) as exhausted:
        decide(holder, position, value, short)

    assert result is True
    assert (exact.work - before, exact.auxiliary_bytes, exact.auxiliary_entries) == paid
    assert exhausted.value.reason == "evaluation_exhausted"


def test_adapter_membership_reuses_only_its_frame_index() -> None:
    holder, position = admitted({"dependentRequired": {"a": ["a"]}, "required": ["a"]})
    exact = remaining_meter("entries", 113)
    short = remaining_meter("entries", 112)
    before = exact.auxiliary_entries
    value = {"a": False}

    result = decide(holder, position, value, exact)
    with pytest.raises(EvaluationRefused) as exhausted:
        decide(holder, position, value, short)

    assert result is True
    assert (exact.work, exact.auxiliary_bytes, exact.auxiliary_entries - before) == (
        14487,
        0,
        113,
    )
    assert exhausted.value.reason == "evaluation_exhausted"


def test_adapter_membership_child_builds_its_own_index() -> None:
    def root_with_child(names: list[str]) -> tuple[PreparedStructuredSchemaInput, int]:
        return admitted(
            {
                "$defs": {"t": {"required": names}},
                "$ref": "#/$defs/t",
                "required": ["a"],
            }
        )

    empty_child, empty_position = root_with_child([])
    indexed_child, indexed_position = root_with_child(["a"])
    empty_meter, indexed_meter = EvaluationMeter(), EvaluationMeter()
    value = {"a": False}

    empty = decide(empty_child, empty_position, value, empty_meter)
    indexed = decide(indexed_child, indexed_position, value, indexed_meter)

    assert empty is True
    assert indexed is True
    assert tuple(
        indexed_cost - empty_cost
        for indexed_cost, empty_cost in zip(
            counters(indexed_meter), counters(empty_meter), strict=True
        )
    ) == (91, 0, 8)


def index_traceback_state(error: BaseException) -> tuple[list[bool], bool]:
    states: list[bool] = []
    built = False
    frame_type = cast("type[object]", vars(structured_schema_adapter)["_Frame"])
    handlers = cast(
        "dict[str, FunctionType]", vars(structured_schema_adapter)["_HANDLERS"]
    )
    codes = {
        handlers["required"].__code__,
        private_function("_frame_object_index").__code__,
    }
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code in codes:
            local = traceback.tb_frame.f_locals
            frame: object = local.get("frame")
            if type(frame) is frame_type:
                states.append(
                    type(object.__getattribute__(frame, "index")) is NativeObjectIndex
                )
            built |= type(local.get("index")) is NativeObjectIndex
        traceback = traceback.tb_next
    return states, built


@pytest.mark.parametrize(("capacity", "stored"), [(10483, False), (10484, True)])
def test_adapter_membership_prepays_index_store(capacity: int, stored: bool) -> None:
    holder, position = admitted({"required": ["a"]})
    meter = remaining_meter("work", capacity)
    before = meter.work

    with pytest.raises(EvaluationRefused) as exhausted:
        decide(holder, position, {"a": False}, meter)
    states, built = index_traceback_state(exhausted.value)

    assert exhausted.value.reason == "evaluation_exhausted"
    assert (meter.work - before, meter.auxiliary_bytes, meter.auxiliary_entries) == (
        capacity,
        0,
        104,
    )
    assert built
    assert states
    assert all(state is stored for state in states)


@pytest.mark.parametrize("keyword", GUARDS)
def test_adapter_all_unfinished_slots_raise_fresh_fixed_internal_errors(
    keyword: str,
) -> None:
    holder, position = admitted({keyword: GUARDS[keyword]})
    error_type = cast(
        "type[RuntimeError]", vars(structured_schema_adapter)["_UnavailableHandler"]
    )

    with pytest.raises(error_type) as first:
        decide(holder, position, {"private-value-canary": []}, EvaluationMeter())
    with pytest.raises(error_type) as second:
        decide(holder, position, None, EvaluationMeter())

    assert len(GUARDS) == 12
    assert first.value is not second.value
    assert first.value.args == second.value.args == ()
    assert str(first.value) == "schema adapter handler unavailable"
    assert repr(first.value) == "_UnavailableHandler()"
    assert first.value.__cause__ is None
    assert first.value.__context__ is None


def test_adapter_availability_error_uses_its_exact_allocation_ticket() -> None:
    holder, position = admitted({"contains": True})
    meter = EvaluationMeter()
    error_type = cast(
        "type[RuntimeError]", vars(structured_schema_adapter)["_UnavailableHandler"]
    )

    with pytest.raises(error_type):
        decide(holder, position, None, meter)

    assert counters(meter) == (10428, 0, 97)


@pytest.mark.parametrize(
    ("keyword", "argument"),
    [("else", False), ("then", False), ("minContains", 2), ("maxContains", 0)],
)
def test_adapter_independent_noops_never_evaluate_unselected_children(
    keyword: str, argument: object
) -> None:
    holder, position = admitted({keyword: argument})
    meter = EvaluationMeter()

    result = decide(holder, position, None, meter)

    assert result is True
    assert meter.auxiliary_entries == 96


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("null", None, True),
        ("boolean", True, True),
        ("integer", True, False),
        ("number", True, False),
        ("integer", 1.5, False),
        ("integer", 10**400, True),
        ("number", 5e-324, True),
        ("string", "private-value-canary", True),
        ("array", [None], True),
        ("object", {"private-key-canary": []}, True),
        (["boolean", "null"], None, True),
        (["integer", "null"], 1.5, False),
    ],
)
def test_adapter_type_is_the_existing_exact_leaf(
    name: object, value: object, expected: bool
) -> None:
    holder, position = admitted({"type": name})
    meter = EvaluationMeter()

    result = decide(holder, position, value, meter)

    assert result is expected


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("target", [True, False, {"type": "null"}])
def test_adapter_reference_calls_use_original_closed_targets(
    keyword: str, target: object
) -> None:
    holder, position = admitted({"$defs": {"t": target}, keyword: "#/$defs/t"})
    meter = EvaluationMeter()

    result = decide(holder, position, None, meter)

    assert result is (target is not False)


def test_adapter_both_reference_occurrences_and_sibling_type_remain_active() -> None:
    schema = {
        "$defs": {"t": True},
        "$ref": "#/$defs/t",
        "$dynamicRef": "#/$defs/t",
        "type": "boolean",
    }
    holder, position = admitted(schema)
    first = EvaluationMeter()
    second = EvaluationMeter()

    matched = decide(holder, position, True, first)
    mismatched = decide(holder, position, None, second)

    assert matched is True
    assert mismatched is False
    assert counters(first) == (18176, 0, 109)
    assert counters(second) == (18172, 0, 110)


def test_adapter_canonical_reference_order_stops_before_unvisited_guard() -> None:
    holder, position = admitted(
        {"$defs": {"t": {"minLength": 2}}, "contains": True, "$ref": "#/$defs/t"}
    )
    meter = EvaluationMeter()
    error_type = cast(
        "type[RuntimeError]", vars(structured_schema_adapter)["_UnavailableHandler"]
    )

    result = decide(holder, position, "😀", meter)
    with pytest.raises(error_type) as later:
        decide(holder, position, "ab", EvaluationMeter())

    assert result is False
    assert str(later.value) == "schema adapter handler unavailable"


def test_adapter_dynamic_reference_precedes_ref_with_a_different_original_target() -> (
    None
):
    holder, position = admitted(
        {
            "$defs": {"deny": False, "guard": {"contains": True}},
            "$ref": "#/$defs/guard",
            "$dynamicRef": "#/$defs/deny",
        }
    )
    meter = EvaluationMeter()

    result = decide(holder, position, None, meter)

    assert result is False


def test_adapter_views_exclude_registry_authority() -> None:
    document = document_for(
        {
            "$id": "https://private-schema.invalid/root",
            "$defs": {"t": {"contains": True}},
            "$ref": "#/$defs/t",
            "private-annotation": {"$ref": "https://private-schema.invalid/lookup"},
        }
    )
    assert validate_type_schemas(document).schemas_valid
    contract = document.type_contracts["example/Value"]
    assert isinstance(contract, CustomTypeContract)
    holder = prepared(preparation_input(dict(contract.schema_)))
    rows = cast(
        "tuple[tuple[int, int, int, int, int], ...]", stored_field(holder, "_positions")
    )
    position = next(
        index for index, row in enumerate(rows) if row[0] == 1 and row[2] == -1
    )
    engine_type = cast("type[object]", vars(structured_schema_adapter)["_CLASS"])
    resolver_type = cast(
        "type[object]", vars(structured_schema_adapter)["_ClosedResolver"]
    )
    context_type = cast("type[object]", vars(structured_schema_adapter)["_Context"])
    registered = cast(
        "dict[str, object]", object.__getattribute__(engine_type, "VALIDATORS")
    )
    error_type = cast(
        "type[RuntimeError]", vars(structured_schema_adapter)["_UnavailableHandler"]
    )
    prior_holder, prior_position = admitted({"contains": True})
    with pytest.raises(error_type) as prior:
        decide(prior_holder, prior_position, None, EvaluationMeter())
    baseline = [
        value
        for value in cast("list[object]", gc.get_objects())
        if type(value) is engine_type or type(value) is context_type
    ]
    baseline_ids = {id(value) for value in baseline}

    with pytest.raises(error_type) as rejected:
        decide(holder, position, {"private-value-canary": []}, EvaluationMeter())
    retained = [
        value
        for value in cast("list[object]", gc.get_objects())
        if id(value) not in baseline_ids
    ]
    engines = [value for value in retained if type(value) is engine_type]
    contexts = [value for value in retained if type(value) is context_type]
    schemas = [
        cast("dict[str, object]", object.__getattribute__(engine, "schema"))
        for engine in engines
    ]
    resolvers = [object.__getattribute__(engine, "_resolver") for engine in engines]

    assert any(type(value) is engine_type for value in baseline)
    assert len(engines) == 2
    assert set(registered) == set(GUARDS) | set(PRIMITIVE_VECTORS) | {
        "type",
        "$ref",
        "$dynamicRef",
        "else",
        "then",
        "minContains",
        "maxContains",
        "dependentRequired",
        "required",
        "uniqueItems",
    }
    assert {tuple(schema) for schema in schemas} == {("$ref",), ("contains",)}
    assert len(contexts) == 1
    assert object.__getattribute__(contexts[0], "status") == "closed"
    assert all(type(resolver) is resolver_type for resolver in resolvers)
    assert resolvers[0] is resolvers[1]
    assert all(
        object.__getattribute__(engine, "_ref_resolver") is None for engine in engines
    )
    del prior, rejected, baseline, retained, engines, contexts, schemas, resolvers


def test_adapter_reference_refusal_remains_distinct_from_completed_false() -> None:
    holder, position = admitted({"$defs": {"t": {"type": "null"}}, "$ref": "#/$defs/t"})
    full = EvaluationMeter()
    assert decide(holder, position, None, full)
    meter = remaining_meter("work", full.work - 100)

    with pytest.raises(EvaluationRefused) as rejected:
        decide(holder, position, None, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert private_function("_reference").__code__ in traceback_codes(rejected.value)


@pytest.mark.parametrize("depth", [64, 65])
def test_adapter_schema_depth_is_independent_of_payload_and_preparation_depth(
    depth: int,
) -> None:
    definitions: dict[str, object] = {
        f"step{index}": {"$ref": f"#/$defs/step{index + 1}"}
        for index in range(depth - 2)
    }
    definitions[f"step{depth - 2}"] = True
    holder, position = admitted({"$defs": definitions, "$ref": "#/$defs/step0"})
    meter = EvaluationMeter()
    assert holder.accounting.input_depth < 32

    if depth == 65:
        with pytest.raises(EvaluationRefused) as rejected:
            decide(holder, position, {"nested": [None]}, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert (
            traceback_codes(rejected.value).count(private_function("_decide").__code__)
            == 65
        )
        return
    result = decide(holder, position, {"nested": [None]}, meter)

    assert result is True


def test_adapter_shared_meter_never_refunds_or_resets_after_mismatch() -> None:
    holder, position = admitted({"type": "boolean"})
    meter = EvaluationMeter()

    first = decide(holder, position, None, meter)
    middle = counters(meter)
    second = decide(holder, position, None, meter)

    assert first is second is False
    assert middle == (10342, 0, 98)
    assert counters(meter) == (20684, 0, 196)


@pytest.mark.parametrize(
    "field", ["policy", "holder", "cursor", "position", "negative", "over"]
)
def test_adapter_defensive_boundary_refuses_inexact_handles(field: str) -> None:
    holder, position = admitted(True)
    meter = EvaluationMeter()
    values: dict[str, object] = {
        "holder": holder,
        "position": position,
        "cursor": native_cursor(None, meter),
        "policy": policy(),
    }
    if field == "negative":
        values["position"] = -1
    elif field == "over":
        values["position"] = 2048
    else:
        values[field] = HostileInteger(position)

    with pytest.raises(EvaluationRefused) as rejected:
        decide_prepared_schema(
            values["holder"],
            values["position"],
            values["cursor"],
            values["policy"],
            meter,
        )

    assert rejected.value.reason == (
        "unsupported_policy" if field == "policy" else "unsupported_schema"
    )


def tracked_adapter_owners() -> set[int]:
    owner_types = (
        NativeCursor,
        ArenaCursor,
        NativeObjectIndex,
        DecimalNumber,
        vars(structured_schema_equality)["_PairFrame"],
        *(
            vars(structured_schema_adapter)[name]
            for name in (
                "_Context",
                "_Frame",
                "_BoundKeyword",
                "_ClosedResolver",
                "_CLASS",
                "_Mismatch",
                "_UnavailableHandler",
            )
        ),
    )
    type_ids = {id(kind) for kind in owner_types}
    engine = vars(structured_schema_adapter)["_CLASS"]
    iterator_code = cast(
        "FunctionType", object.__getattribute__(engine, "iter_errors")
    ).__code__
    return {
        id(cast("object", value))
        for value in cast("list[object]", gc.get_objects())
        if id(type(value)) in type_ids
        or (type(value) is GeneratorType and value.gi_code is iterator_code)
    }


Outcome = Literal[
    "true", "false", "refusal", "recursive_refusal", "unavailable", "unexpected"
]
MembershipOutcome = Literal[
    "membership_true",
    "membership_false",
    "membership_refusal",
    "membership_unexpected",
]


def invalid_required_child(
    holder: PreparedStructuredSchemaInput, position: int
) -> PreparedStructuredSchemaInput:
    positions = cast(
        "tuple[tuple[int, int, int, int, int], ...]", stored_field(holder, "_positions")
    )
    arena = list(arena_nodes(holder))
    schema = arena[positions[position][1]]
    argument_id = next(
        value_id
        for key_id, value_id in schema.members
        if arena[key_id].value == "required"
    )
    arena[argument_id] = replace(arena[argument_id], children=(len(arena),))
    return replace(holder, _arena=tuple(arena))


def membership_owned_call(outcome: MembershipOutcome) -> bool:
    holder, position = admitted({"required": ["a"], "title": "private-schema-canary"})
    meter = (
        remaining_meter("entries", 104)
        if outcome == "membership_refusal"
        else EvaluationMeter()
    )
    if outcome == "membership_unexpected":
        holder = invalid_required_child(holder, position)
    value = {"b" if outcome == "membership_false" else "a": "private-payload-canary"}
    return decide(holder, position, value, meter)


def owned_call(outcome: Outcome) -> bool:
    if outcome == "recursive_refusal":
        holder, position = admitted(
            {"enum": [[None], [False]], "title": "private-schema-canary"}
        )
        return decide(holder, position, [None], remaining_meter("entries", 103))
    schema: dict[str, object] = (
        {"contains": True} if outcome == "unavailable" else {"type": "integer"}
    )
    schema["title"] = "private-schema-canary"
    holder, position = admitted(schema)
    meter = (
        remaining_meter("work", 14533) if outcome == "refusal" else EvaluationMeter()
    )
    if outcome == "unexpected":
        rows = cast(
            "tuple[tuple[int, int, int, int, int], ...]",
            stored_field(holder, "_positions"),
        )
        invalid = list(rows)
        invalid[position] = (1, len(arena_nodes(holder)), -1, -1, -1)
        holder = replace(holder, _positions=tuple(invalid))
    return decide(holder, position, 1.5 if outcome == "false" else 1.0, meter)


@pytest.mark.parametrize("outcome", ["true", "false"])
def test_adapter_completed_calls_release_all_owned_records(outcome: Outcome) -> None:
    gc.collect()
    before = tracked_adapter_owners()

    result = owned_call(outcome)
    gc.collect()
    after = tracked_adapter_owners()

    assert result is (outcome == "true")
    assert after == before


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("membership_true", True), ("membership_false", False)],
)
def test_adapter_completed_membership_releases_indexes(
    outcome: MembershipOutcome, expected: bool
) -> None:
    gc.collect()
    before = tracked_adapter_owners()

    result = membership_owned_call(outcome)
    gc.collect()
    after = tracked_adapter_owners()

    assert result is expected
    assert after == before


@pytest.mark.parametrize(
    "outcome", ["refusal", "recursive_refusal", "unavailable", "unexpected"]
)
def test_adapter_disposed_tracebacks_release_error_and_operation_records(
    outcome: Outcome,
) -> None:
    error_type = (
        EvaluationRefused
        if outcome in ("refusal", "recursive_refusal")
        else (
            IndexError
            if outcome == "unexpected"
            else cast(
                "type[RuntimeError]",
                vars(structured_schema_adapter)["_UnavailableHandler"],
            )
        )
    )
    gc.collect()
    before = tracked_adapter_owners()

    with pytest.raises(error_type) as rejected:
        owned_call(outcome)
    held = tracked_adapter_owners() - before
    safe_text = str(rejected.value), repr(rejected.value)
    safe_owner_text = tuple(
        repr(value) for value in gc.get_objects() if id(value) in held
    )
    del rejected
    gc.collect()
    after = tracked_adapter_owners()

    assert held
    assert after == before
    assert all(
        "private-schema-canary" not in text for text in safe_text + safe_owner_text
    )


@pytest.mark.parametrize("outcome", ["membership_refusal", "membership_unexpected"])
def test_adapter_failed_membership_releases_installed_index(
    outcome: MembershipOutcome,
) -> None:
    error_type = EvaluationRefused if outcome == "membership_refusal" else IndexError
    gc.collect()
    before = tracked_adapter_owners()

    with pytest.raises(error_type) as rejected:
        membership_owned_call(outcome)
    held = tracked_adapter_owners() - before
    states, built = index_traceback_state(rejected.value)
    reason = (
        rejected.value.reason if isinstance(rejected.value, EvaluationRefused) else None
    )
    chained = (
        rejected.value.__cause__ is not None or rejected.value.__context__ is not None
    )
    safe_text = (str(rejected.value), repr(rejected.value)) + tuple(
        repr(value) for value in gc.get_objects() if id(value) in held
    )
    del rejected
    gc.collect()
    after = tracked_adapter_owners()

    assert built
    assert states
    assert all(states)
    assert "NativeObjectIndex()" in safe_text
    assert reason == (
        "evaluation_exhausted" if outcome == "membership_refusal" else None
    )
    assert not chained
    assert after == before
    for marker in ("private-schema-canary", "private-payload-canary"):
        assert all(marker not in text for text in safe_text)


class _Drive(Protocol):
    def __call__(
        self, engine: object, value: NativeCursor, count: int, meter: EvaluationMeter
    ) -> bool: ...


def test_adapter_driver_rejects_real_library_yield_and_closes_its_iterator() -> None:
    engine = Draft202012Validator({"type": "string"})
    meter = EvaluationMeter()
    cursor = native_cursor(None, meter)
    drive = cast("_Drive", private_function("_drive"))

    with pytest.raises(RuntimeError) as rejected:
        drive(engine, cursor, 1, meter)
    traceback = rejected.value.__traceback__
    while (
        traceback is not None
        and traceback.tb_frame.f_code is not private_function("_drive").__code__
    ):
        traceback = traceback.tb_next
    assert traceback is not None
    iterator = cast(
        "GeneratorType[object, None, None]", traceback.tb_frame.f_locals["iterator"]
    )

    assert str(rejected.value) == "schema adapter iterator yielded"
    assert iterator.gi_frame is None
    del iterator, traceback, rejected


def test_adapter_emits_no_content_or_operation_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    holder, position = admitted({"type": "boolean", "title": "private-schema-canary"})
    capsys.readouterr()

    result = decide(holder, position, {"private-value-canary": []}, EvaluationMeter())
    output = capsys.readouterr()

    assert result is False
    assert output.out == output.err == ""
