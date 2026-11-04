import cProfile
from typing import cast

import pytest

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_schema_preparation import prepare_graph_schema
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_schema_evaluation_policy import EvaluationRefused
from kora.ir.structured_schema_input import (
    PreparedStructuredSchemaInput,
    StructuredSchemaPreparationRefusal,
)
from tests.engine.graph_schema_cases import (
    declaration,
    input_size,
    saved_types,
    schema_candidate,
)
from tests.ir.structured_schema_cases import normalized_input, reachable_container_ids
from tests.ir.support import deep_data


def test_graph_schema_preparation_selects_exact_closure() -> None:
    ir_text, records = saved_types(
        {
            "example/Base": declaration({"type": "object"}),
            "example/Declared": declaration(
                {
                    "$defs": {
                        "unused": {
                            "$dynamicRef": (
                                "urn:kora:artifact:example:Dependency#/$defs/value"
                            )
                        }
                    },
                    "$recursiveRef": "urn:kora:artifact:example:Inert#",
                    "annotation": {"$ref": "urn:kora:artifact:example:Unrelated#"},
                },
                "example/Base",
            ),
            "example/Dependency": declaration(
                {
                    "$defs": {
                        "value": True,
                        "unused": {"$ref": "urn:kora:artifact:example:Transitive#"},
                    }
                },
                "example/DependencyParent",
            ),
            "example/DependencyParent": declaration({}, None),
            "example/Inline": declaration({}),
            "example/Transitive": declaration({}),
            "example/Inert": declaration({"pattern": "unsupported"}),
            "example/InlineInert": declaration({"pattern": "unsupported"}),
            "example/Unrelated": declaration({"pattern": "unsupported"}),
        }
    )
    candidate = schema_candidate(
        {
            "$defs": {"unused": {"$ref": "urn:kora:artifact:example:Inline#"}},
            "$recursiveRef": "urn:kora:artifact:example:InlineInert#",
            "annotation": {"$ref": "urn:kora:artifact:example:Unrelated#"},
        },
        "example/Declared",
    )
    caller_inputs = (records, candidate)
    caller_ids = reachable_container_ids(caller_inputs)

    result = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=100_000
    )

    assert isinstance(result, PreparedStructuredSchemaInput)
    normalized = cast(JsonObject, normalized_input(result))
    types = cast(list[JsonObject], normalized["type_contracts"])
    documents = cast(list[JsonObject], normalized["documents"])
    assert [row["name"] for row in types] == [
        "StructuredOutput",
        "example/Base",
        "example/Declared",
        "example/Dependency",
        "example/DependencyParent",
        "example/Inline",
        "example/Transitive",
    ]
    assert normalized["constraint_documents"] == [0, 1, 2, 7]
    assert normalized["checker_document"] == 2
    assert documents[-1] == candidate["inline_schema"]
    assert len(documents) == 8
    assert all("references" not in row for row in types)
    assert not caller_ids.intersection(reachable_container_ids(result))


@pytest.mark.parametrize(
    "schema", [None, True, False, {}], ids=["absent", "true", "false", "object"]
)
def test_graph_schema_preparation_preserves_inline_roots(
    schema: JsonObject | bool | None,
) -> None:
    ir_text, records = saved_types({})
    candidate = schema_candidate(schema)

    result = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=100_000
    )

    assert isinstance(result, PreparedStructuredSchemaInput)
    normalized = cast(JsonObject, normalized_input(result))
    documents = cast(list[JsonObject], normalized["documents"])
    assert normalized["checker_document"] == (0 if schema is None else 1)
    assert normalized["constraint_documents"] == ([0] if schema is None else [0, 1])
    if schema is not None:
        assert documents[-1]["schema"] == schema


@pytest.mark.parametrize(
    "schema", [{"pattern": "x"}, {"$ref": "#"}, {"description": "\ud800"}]
)
def test_graph_schema_preparation_retains_real_refusals(schema: JsonObject) -> None:
    ir_text, records = saved_types({})
    candidate = schema_candidate(schema)

    result = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=100_000
    )

    assert result == StructuredSchemaPreparationRefusal("unsupported_schema")


@pytest.mark.parametrize(
    "value", ["example/Missing", "List[StructuredOutput]", None, 1, [], {}]
)
def test_graph_schema_preparation_refuses_unsupported_selected_types(
    value: JsonValue,
) -> None:
    ir_text, records = saved_types({})
    candidate = schema_candidate()
    candidate["declared_type"] = value

    result = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=100_000
    )

    assert result == StructuredSchemaPreparationRefusal("unsupported_schema")


@pytest.mark.parametrize("field", ["execution_semantics", "checker_binding"])
@pytest.mark.parametrize("value", [None, "private unsupported", 1])
def test_graph_schema_preparation_requires_explicit_bindings(
    field: str, value: JsonValue
) -> None:
    ir_text, records = saved_types({})
    candidate = schema_candidate()
    candidate[field] = value

    result = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=100_000
    )

    assert result == StructuredSchemaPreparationRefusal("unsupported_binding")


@pytest.mark.parametrize(
    "change", ["missing", "extra", "policy", "parameter", "boolean", "ten-only"]
)
def test_graph_schema_preparation_checks_complete_policy(change: str) -> None:
    ir_text, records = saved_types({})
    candidate = schema_candidate()
    policy = cast(JsonObject, candidate["schema_validation"])
    parameters = cast(JsonObject, policy["parameters"])
    if change == "missing":
        policy.pop("parameters")
    elif change == "extra":
        policy["extra"] = "private"
    elif change == "policy":
        policy["policy"] = "private"
    elif change == "boolean":
        parameters["max_input_bytes"] = True
    elif change == "ten-only":
        for name in (
            "max_numeric_intermediate_bits",
            "max_evaluation_work",
            "max_evaluation_depth",
        ):
            parameters.pop(name)
    else:
        parameters["max_input_bytes"] = 1

    with pytest.raises(EvaluationRefused) as failure:
        prepare_graph_schema(ir_text, records, candidate, max_encoded_bytes=100_000)

    assert failure.value.reason == "unsupported_policy"
    assert str(failure.value) == "unsupported_policy"


@pytest.mark.parametrize(
    "change", ["type", "missing", "extra", "inline-root", "inline-ref", "inline-shape"]
)
def test_graph_schema_preparation_checks_explicit_inputs(change: str) -> None:
    ir_text, records = saved_types({})
    candidate = schema_candidate({})
    inline = cast(JsonObject, candidate["inline_schema"])
    if change == "type":
        cast(JsonObject, records[0])["parent"] = "example/Missing"
    elif change == "missing":
        candidate.pop("declared_type")
    elif change == "extra":
        candidate["extra"] = "private"
    elif change == "inline-root":
        inline["schema"] = "private"
    elif change == "inline-ref":
        inline["schema"] = {"$ref": "urn:kora:artifact:example:Missing#"}
    else:
        inline.pop("source")

    with pytest.raises(GraphValueRejected) as failure:
        prepare_graph_schema(ir_text, records, candidate, max_encoded_bytes=100_000)

    assert failure.value.reason is GraphValueFailure.INVALID
    assert str(failure.value) == "invalid graph value representation"


@pytest.mark.parametrize("dimension", ["documents", "depth", "work"])
def test_graph_schema_preparation_keeps_independent_capacity(dimension: str) -> None:
    contracts: JsonObject = {}
    parent = "StructuredOutput"
    count = 65 if dimension == "documents" else 4 if dimension == "work" else 1
    schema: JsonObject = (
        {"annotation": {f"{index:04d}" + "x" * 396: None for index in range(512)}}
        if dimension == "work"
        else {"annotation": cast(JsonValue, deep_data(40))}
        if dimension == "depth"
        else {}
    )
    for index in range(count):
        name = f"example/Type{index:03d}"
        contracts[name] = declaration(schema, parent)
        parent = name
    ir_text, records = saved_types(contracts)
    candidate = schema_candidate(declared=parent)

    result = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=10_000_000
    )

    assert result == StructuredSchemaPreparationRefusal("preparation_exhausted")


def test_graph_schema_preparation_admits_complete_input() -> None:
    ir_text, records = saved_types(
        {"example/Unused": declaration({"description": "\0\ud800"})}
    )
    candidate = schema_candidate({"description": 'line\n"\\\U0001f600'})
    exact_size = input_size(ir_text, records, candidate)
    profile = cProfile.Profile()

    result = prepare_graph_schema(
        ir_text, records, candidate, max_encoded_bytes=exact_size
    )
    profile.enable()
    try:
        with pytest.raises(GraphValueRejected) as failure:
            prepare_graph_schema(
                ir_text, records, candidate, max_encoded_bytes=exact_size - 1
            )
    finally:
        profile.disable()

    assert isinstance(result, PreparedStructuredSchemaInput)
    assert failure.value.reason is GraphValueFailure.BUDGET
    names = {getattr(entry.code, "co_name", None) for entry in profile.getstats()}
    assert not names.intersection(
        {
            "read_graph_type_contracts",
            "select_records",
            "prepare_structured_schema_input",
            "object",
        }
    )


def test_graph_schema_preparation_detaches_owned_inputs() -> None:
    ir_text, records = saved_types({})
    candidate = schema_candidate({"description": "original"})
    before = input_size(ir_text, records, candidate)

    result = prepare_graph_schema(ir_text, records, candidate, max_encoded_bytes=before)
    assert isinstance(result, PreparedStructuredSchemaInput)
    records.clear()
    cast(JsonObject, cast(JsonObject, candidate["inline_schema"])["schema"])[
        "description"
    ] = "changed"

    documents = cast(
        list[JsonObject], cast(JsonObject, normalized_input(result))["documents"]
    )
    assert cast(JsonObject, documents[-1]["schema"])["description"] == "original"
    assert len(documents) == 2
