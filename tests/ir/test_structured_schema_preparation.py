import math
from collections import UserDict
from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest

from kora.ir import StructuredOutput, validate_type_schemas
from kora.ir.structured_json import encode_snapshot, snapshot
from kora.ir.structured_schema_input import PreparedStructuredSchemaInput
from tests.ir.structured_schema_cases import (
    HostileDictionary,
    HostileInteger,
    HostileString,
    arena_nodes,
    builtin,
    contract,
    native_counts,
    normalized_input,
    parameters,
    preparation_input,
    prepared,
    reachable_container_ids,
    refusal,
    source,
)
from tests.ir.type_schema_cases import document_for


def test_structured_output_preparation_normalizes_complete_input() -> None:
    root = builtin()
    parent = contract({"type": "object"}, "example/Base")
    declared = contract(
        {"$ref": "urn:kora:artifact:example:Shared"}, parent="example/Base"
    )
    shared = contract(False, "example/Shared")
    location = {"file": "saved.py", "line": 3, "authored": {"extension": [0, "🌍"]}}
    cast("dict[str, object]", declared["source"])["location"] = location
    inline_source = source("inline", "inline_schema")
    inline_source["path"] = ["graph", "nodes", 0, "config", "output_schema"]
    inline: dict[str, object] = {"source": inline_source, "schema": False}
    records = [shared, declared, root, parent]
    candidate = preparation_input({}, records=records, inline=inline)
    ordered = [root, parent, declared, shared]
    expected: dict[str, object] = {
        "declared_type": "example/Declared",
        "execution_semantics": "kora.execution.v1",
        "checker_binding": "kora.structured-output-view.v1",
        "type_contracts": [
            {
                "name": record["name"],
                "builtin_revision": record["builtin_revision"],
                "parent": record["parent"],
                "behavior": record["behavior"],
                "descriptor": record["descriptor"],
                "schema_document": index,
            }
            for index, record in enumerate(ordered)
        ],
        "documents": [
            {"source": record["source"], "schema": record["schema"]}
            for record in ordered
        ]
        + [inline],
        "constraint_documents": [0, 1, 2, 4],
        "checker_document": 2,
        "reference_targets": [
            {
                "document": 2,
                "path": [],
                "keyword": "$ref",
                "target_document": 3,
                "target_path": [],
            }
        ],
        "preparation_parameters": parameters(),
    }
    expected_nodes, expected_depth = native_counts(expected)
    expected_bytes = encode_snapshot(snapshot(expected))

    result = prepared(candidate)

    assert normalized_input(result) == expected
    assert result.accounting.input_bytes == len(expected_bytes)
    assert result.accounting.input_nodes == expected_nodes
    assert len(arena_nodes(result)) == expected_nodes
    assert result.accounting.input_depth == expected_depth
    assert result.accounting.documents == 5
    assert result.accounting.positions == 5
    assert result.accounting.references == 1


@pytest.mark.parametrize("inline_present", [False, True], ids=["builtin", "inline"])
def test_structured_output_preparation_assigns_exact_builtin_checker_role(
    inline_present: bool,
) -> None:
    inline = (
        {"source": source("inline", "inline_schema"), "schema": False}
        if inline_present
        else None
    )
    candidate = preparation_input(
        {}, records=[builtin()], declared_type="StructuredOutput", inline=inline
    )

    result = prepared(candidate)
    normalized = cast("dict[str, object]", normalized_input(result))

    assert normalized["checker_document"] == int(inline_present)
    assert normalized["constraint_documents"] == ([0, 1] if inline_present else [0])


def test_structured_output_preparation_normalizes_source_and_map_order() -> None:
    first = preparation_input(
        {"properties": {"z": False, "a": {"type": "string"}}, "title": "saved"}
    )
    second = preparation_input(
        {"title": "saved", "properties": {"a": {"type": "string"}, "z": False}},
    )
    cast("list[dict[str, object]]", second.type_contracts).reverse()

    left = prepared(first)
    right = prepared(second)

    assert left == right
    assert left.accounting.documents == 2
    assert left.accounting.positions == 4
    assert left.accounting.references == 0


def test_structured_output_preparation_owns_originals() -> None:
    schema: dict[str, object] = {"properties": {"value": {"type": "string"}}}
    location: dict[str, object] = {"file": "original.py", "extension": [1, "saved"]}
    record = contract(schema)
    cast("dict[str, object]", record["source"])["location"] = location
    candidate = preparation_input(schema, records=[builtin(), record])
    input_ids = reachable_container_ids(candidate)

    result = prepared(candidate)
    retained_ids = reachable_container_ids(result)
    expected = prepared(candidate)
    schema.clear()
    schema["pattern"] = "changed"
    location["extension"] = ["changed"]
    record["name"] = "example/Changed"
    cast("dict[str, int]", candidate.preparation_parameters).clear()

    assert result == expected
    assert input_ids.isdisjoint(retained_ids)
    assert result.accounting.positions == 3


def test_structured_output_preparation_counts_each_shared_native_occurrence() -> None:
    shared: dict[str, object] = {"annotation": ["same", 0]}
    alias = preparation_input({"const": [shared, shared]})
    copies = preparation_input(
        {"const": [{"annotation": ["same", 0]}, {"annotation": ["same", 0]}]}
    )

    repeated = prepared(alias)
    detached = prepared(copies)

    assert repeated == detached
    assert repeated.accounting.positions == 2


@pytest.mark.parametrize("kind", ["object", "array", "metadata"], ids=str)
def test_structured_output_preparation_rejects_native_cycles(kind: str) -> None:
    cycle: dict[str, object] = {}
    cycle["child"] = cycle
    array: list[object] = []
    array.append(array)
    schema: object = {"annotation": array if kind == "array" else cycle}
    candidate = preparation_input(schema)
    if kind == "metadata":
        records = cast("list[dict[str, object]]", candidate.type_contracts)
        cast("dict[str, object]", records[1]["source"])["location"] = cycle

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("str-subclass", HostileString("canary")),
        ("int-subclass", HostileInteger(1)),
        ("dict-subclass", HostileDictionary()),
        ("mapping", UserDict({"value": 1})),
        ("tuple", (1, 2)),
        ("bytes", b"canary"),
        ("non-string-key", {1: "value"}),
        ("subclass-key", {HostileString("key"): 1}),
        ("nan", math.nan),
        ("positive-infinity", math.inf),
        ("negative-infinity", -math.inf),
        ("surrogate-value", "\ud800"),
        ("surrogate-key", {"\udfff": 1}),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_structured_output_preparation_native_domain_has_no_coercion(
    name: str, value: object
) -> None:
    candidate = preparation_input({"annotation": value})

    reason = refusal(candidate)

    assert name
    assert reason == "unsupported_schema"


@pytest.mark.parametrize("field", ["schema", "source", "descriptor", "behavior"])
def test_structured_output_preparation_checks_all_retained_text(field: str) -> None:
    record = builtin()
    if field == "schema":
        cast("dict[str, object]", record[field])["annotation"] = "\ud800"
    elif field == "source":
        cast("dict[str, object]", record[field])["location"] = {"extension": "\ud800"}
    elif field == "descriptor":
        cast("dict[str, object]", record[field])["extension"] = "\ud800"
    else:
        cast("dict[str, object]", record[field])["summary"] = "\ud800"
    candidate = preparation_input(
        {}, records=[record], declared_type="StructuredOutput"
    )

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize(
    "binding",
    ["execution_semantics", "checker_binding", "preparation_parameters"],
)
def test_preparation_rejects_unsupported_binding(binding: str) -> None:
    candidate = preparation_input({})
    if binding == "execution_semantics":
        candidate = replace(candidate, execution_semantics="future.execution.v2")
    elif binding == "checker_binding":
        candidate = replace(candidate, checker_binding="future.checker.v2")
    else:
        changed = parameters()
        changed["max_numeric_bits"] -= 1
        candidate = replace(candidate, preparation_parameters=changed)

    reason = refusal(candidate)

    assert reason == "unsupported_binding"


@pytest.mark.parametrize("change", ["missing", "extra", "boolean", "subclass"])
def test_preparation_requires_exact_ten_parameters(change: str) -> None:
    limits: dict[str, object] = dict(parameters())
    if change == "missing":
        del limits["max_input_bytes"]
    elif change == "extra":
        limits["schema_validation"] = "reserved"
    elif change == "boolean":
        limits["max_input_bytes"] = True
    else:
        limits["max_input_bytes"] = HostileInteger(1_048_576)
    candidate = replace(preparation_input({}), preparation_parameters=limits)

    reason = refusal(candidate)

    assert reason == "unsupported_binding"


@pytest.mark.parametrize(
    "change",
    ["list", "unrelated", "revision", "cycle", "missing-parent", "duplicate", "extra"],
)
def test_structured_output_preparation_rejects_ineligible_types(change: str) -> None:
    root = builtin()
    declared = contract({})
    records = [root, declared]
    selected = "example/Declared"
    if change == "list":
        selected = "List[example/Declared]"
    elif change == "unrelated":
        declared["parent"] = None
    elif change == "revision":
        root["builtin_revision"] = "2"
    elif change == "cycle":
        declared["parent"] = "example/Declared"
    elif change == "missing-parent":
        declared["parent"] = "example/Missing"
    elif change == "duplicate":
        records.append(contract({}))
    else:
        records.append(contract({}, "example/Unused"))
    candidate = preparation_input({}, records=records, declared_type=selected)

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize(
    "change",
    [
        "kind",
        "path",
        "negative-index",
        "location",
        "array-location",
        "source-field",
        "record-field",
    ],
)
def test_structured_output_preparation_rejects_inconsistent_source_records(
    change: str,
) -> None:
    record = contract({})
    original = cast("dict[str, object]", record["source"])
    if change == "kind":
        original["kind"] = "inline_schema"
    elif change == "path":
        original["path"] = ("source",)
    elif change == "negative-index":
        original["path"] = ["source", -1]
    elif change == "location":
        original["location"] = "invented"
    elif change == "array-location":
        original["location"] = []
    elif change == "source-field":
        original["authority"] = True
    else:
        record["reference_targets"] = []
    candidate = preparation_input({}, records=[builtin(), record])

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


def test_structured_output_preparation_is_immutable_and_exposes_no_evaluation() -> None:
    candidate = preparation_input({"minimum": 5, "maximum": 1})
    immutable_attribute = "accounting"

    result = prepared(candidate)

    assert isinstance(result, PreparedStructuredSchemaInput)
    assert not any(
        hasattr(result, name)
        for name in (
            "value",
            "validate",
            "is_valid",
            "digest",
            "summary",
            "canonical_bytes",
        )
    )
    with pytest.raises(FrozenInstanceError):
        setattr(result, immutable_attribute, result.accounting)


def test_structured_output_preparation_preserves_ordinary_structural_ir() -> None:
    document = document_for({"pattern": "a*"})
    native = StructuredOutput({"value": "ordinary"})

    structural = validate_type_schemas(document)
    reason = refusal(preparation_input({"pattern": "a*"}))

    assert structural.schemas_valid
    assert native.value == {"value": "ordinary"}
    assert reason == "unsupported_schema"
