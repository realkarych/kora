import json
from dataclasses import replace
from typing import cast

import pytest

from kora.ir.standard_output_preparation import (
    StandardOutputInput,
    prepare_standard_output,
)
from kora.ir.standard_schema_policy import SchemaRefused
from kora.ir.structured_output import StructuredOutputValidationError
from tests.ir.standard_output_cases import output_input
from tests.ir.structured_schema_cases import builtin, contract, source
from tests.plugins.artifact_type.test_structured_output import HostileValue


def decoded(value: StandardOutputInput) -> dict[str, object]:
    return cast(
        "dict[str, object]", json.loads(prepare_standard_output(value).source_bytes)
    )


def test_effective_source_keeps_original_roles_and_origins() -> None:
    root = builtin()
    parent = contract({"type": "number"}, "example/Parent")
    declared = contract({"minimum": 2}, parent="example/Parent")
    referenced = contract(
        {"type": "string", "$defs": {"number": {"type": "number"}}}, "example/Reference"
    )
    declared["schema"] = {
        "$ref": "urn:kora:artifact:example:Reference#/$defs/number",
        "minimum": 2,
    }
    inline = {"source": source("inline", "inline_schema"), "schema": {"maximum": 10}}
    candidate = output_input(
        records=[declared, root, referenced, parent], inline=inline
    )

    result = decoded(candidate)

    assert result["constraints"] == [1, 3, 0, 4]
    documents = cast("list[dict[str, object]]", result["documents"])
    assert [item["name"] for item in documents] == [
        "example/Declared",
        "StructuredOutput",
        "example/Reference",
        "example/Parent",
        None,
    ]
    assert documents[2]["schema"] == referenced["schema"]
    metadata = cast("dict[str, object]", result["metadata"])
    assert metadata["checker_document"] == 0
    assert metadata["inline_source"] == inline["source"]
    assert (
        cast("list[dict[str, object]]", metadata["type_contracts"])[0]["source"]
        == declared["source"]
    )


def test_builtin_inline_is_checker_without_replacing_builtin_constraint() -> None:
    value = output_input(
        records=[builtin()],
        declared="StructuredOutput",
        inline={"source": source("inline", "inline_schema"), "schema": False},
    )

    result = decoded(value)

    assert result["constraints"] == [0, 1]
    assert cast("dict[str, object]", result["metadata"])["checker_document"] == 1


@pytest.mark.parametrize(
    "defect",
    [
        "missing_parent",
        "missing_reference",
        "extra_record",
        "duplicate",
        "parent_cycle",
        "wrong_root",
        "list",
        "bad_source",
        "unsupported_hidden",
        "reference_cycle",
    ],
)
def test_effective_source_refuses_incomplete_or_unsupported_closure(
    defect: str,
) -> None:
    root = builtin()
    selected = contract(True)
    records = [root, selected]
    declared = "example/Declared"
    if defect == "missing_parent":
        selected["parent"] = "example/Missing"
    elif defect == "missing_reference":
        selected["schema"] = {"$ref": "urn:kora:artifact:example:Missing"}
    elif defect == "extra_record":
        records.append(contract(True, "example/Extra"))
    elif defect == "duplicate":
        records.append(contract(True))
    elif defect == "parent_cycle":
        selected["parent"] = declared
    elif defect == "wrong_root":
        selected["parent"] = None
    elif defect == "list":
        declared = "List[example/Declared]"
    elif defect == "bad_source":
        selected["source"] = source("wrong", "inline_schema")
    elif defect == "unsupported_hidden":
        selected["schema"] = {"$defs": {"hidden": {"pattern": "private"}}}
    else:
        selected["schema"] = {"not": {"$ref": "#"}}
    candidate = output_input(records=records, declared=declared)

    with pytest.raises(SchemaRefused) as caught:
        prepare_standard_output(candidate)

    assert caught.value.reason == "unsupported_schema"


@pytest.mark.parametrize(
    "field", ["execution_semantics", "checker_binding", "schema_validation"]
)
def test_effective_source_requires_exact_header(field: str) -> None:
    candidate = replace(output_input(), **{field: HostileValue()})

    with pytest.raises(SchemaRefused) as caught:
        prepare_standard_output(candidate)

    assert caught.value.reason == "unsupported_policy"


@pytest.mark.parametrize("defect", ["order", "boolean", "missing", "extra", "identity"])
def test_effective_source_requires_complete_ordered_policy(defect: str) -> None:
    candidate = output_input()
    policy = cast("dict[str, object]", candidate.schema_validation)
    limits = cast("dict[str, object]", policy["parameters"])
    if defect == "order":
        policy["parameters"] = dict(reversed(list(limits.items())))
    elif defect == "boolean":
        limits["max_value_depth"] = True
    elif defect == "missing":
        limits.pop("max_value_depth")
    elif defect == "extra":
        limits["unrecognized"] = 1
    else:
        policy["policy"] = "kora.structured-output-schema-policy.v1"

    with pytest.raises(SchemaRefused) as caught:
        prepare_standard_output(candidate)

    assert caught.value.reason == "unsupported_policy"


def test_effective_source_checks_descriptor_before_behavior() -> None:
    original = builtin()
    cast("dict[str, object]", original["descriptor"])["canonicalization"] = "unknown"
    candidate = output_input(records=[original], declared="StructuredOutput")

    with pytest.raises(StructuredOutputValidationError) as caught:
        prepare_standard_output(candidate)

    assert caught.value.violation.code == "SO005"


@pytest.mark.parametrize(
    "value", [HostileValue(), {"x": HostileValue()}, [HostileValue()]]
)
def test_effective_source_rejects_non_native_before_indexing(value: object) -> None:
    candidate = output_input(value)

    with pytest.raises(SchemaRefused) as caught:
        prepare_standard_output(candidate)

    assert caught.value.reason == "unsupported_schema"


def test_effective_source_retains_equal_but_distinct_roots() -> None:
    same = {"$defs": {"number": {"type": "number"}}, "$ref": "#/$defs/number"}
    parent = contract(same, "example/Parent")
    selected = contract(dict(same), parent="example/Parent")
    candidate = output_input(records=[builtin(), parent, selected])

    result = decoded(candidate)

    assert result["constraints"] == [0, 1, 2]
    documents = cast("list[dict[str, object]]", result["documents"])
    assert documents[1]["schema"] == documents[2]["schema"]
    assert documents[1]["schema"] is not documents[2]["schema"]
