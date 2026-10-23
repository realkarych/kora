import json
from typing import cast

import pytest
from pydantic import MISSING, TypeAdapter, ValidationError

from kora.ir import InputRef, IrSourceLocation, OutputDecl, SourceLocation, export_ir
from kora.ir.json import JsonObject
from kora.ir.record import IrRecord
from kora.ir.scalars import (
    ArtifactType,
    IrVersion,
    RetentionPeriod,
    SecretRef,
    TypeContractName,
)
from tests.ir.declaration_cases import INVALID, MINIMAL, POPULATED
from tests.ir.support import SCHEMA_PATH, deep_data

SCALAR_ADAPTERS: dict[str, TypeAdapter[str]] = {
    "IrVersion": TypeAdapter(IrVersion),
    "TypeContractName": TypeAdapter(TypeContractName),
    "RetentionPeriod": TypeAdapter(RetentionPeriod),
    "SecretRef": TypeAdapter(SecretRef),
    "ArtifactType": TypeAdapter(ArtifactType),
}


@pytest.mark.parametrize("model", list(MINIMAL))
def test_ir_declarations_round_trip_and_refuse_invalid(
    model: type[IrRecord],
) -> None:
    documents = [MINIMAL[model], POPULATED[model]]

    exported = [export_ir(model.model_validate(document)) for document in documents]
    with pytest.raises(ValidationError):
        model.model_validate(INVALID[model])

    assert exported == documents


def test_ir_source_metadata_is_distinct_from_diagnostic_location() -> None:
    document = {"file": "file:///private/secret\n", "line": 1, "x-label": "raw"}

    source = IrSourceLocation.model_validate(document)
    with pytest.raises(ValidationError):
        SourceLocation.model_validate(document)

    assert export_ir(source) == document
    assert not isinstance(source, SourceLocation)


@pytest.mark.parametrize("line", [False, None, 0, -1, 1.1, "1"])
def test_ir_source_line_is_a_positive_json_integer(line: object) -> None:
    document = {"file": "", "line": line}

    with pytest.raises(ValidationError):
        IrSourceLocation.model_validate(document)


def test_ir_source_and_concurrency_accept_integral_json_numbers() -> None:
    from kora.ir import Concurrency

    source = {"file": "/raw/source.py", "line": 1.0}

    parsed = IrSourceLocation.model_validate(source)
    concurrency = Concurrency.model_validate({"max_running_nodes": 2.0})

    assert export_ir(parsed) == {"file": "/raw/source.py", "line": 1}
    assert export_ir(concurrency) == {"max_running_nodes": 2}


def test_ir_input_requires_only_type_and_refuses_internal_aliases() -> None:
    document = {
        "type": "List[List[custom/Text]]",
        "from": "$previous_attempt",
        "optional": False,
    }

    parsed = InputRef.model_validate(document)
    minimal = InputRef.model_validate({"type": "Text"})
    with pytest.raises(ValidationError):
        InputRef.model_validate({"type": "Text", "from_": "node"})
    with pytest.raises(ValidationError):
        OutputDecl.model_validate({"type": "Text", "schema_": {}})

    assert export_ir(parsed) == document
    assert minimal.from_ is MISSING


@pytest.mark.parametrize("depth", [17, 64, 128, 255, 256, 512, 1024])
def test_ir_output_schema_remains_detached_open_data(depth: int) -> None:
    data = deep_data(depth)
    document = {"type": "StructuredOutput", "schema": data}

    parsed = OutputDecl.model_validate(document)
    first = export_ir(parsed)
    data["new"] = False
    cast("JsonObject", first["schema"])["exported"] = True
    second = export_ir(parsed)

    assert "new" not in cast("JsonObject", second["schema"])
    assert "exported" not in cast("JsonObject", second["schema"])


@pytest.mark.parametrize(
    ("scalar", "value", "valid"),
    [
        ("IrVersion", "1.000", True),
        ("IrVersion", "1.0\n", False),
        ("TypeContractName", "ns/Text", True),
        ("TypeContractName", "ns/Text\n", False),
        ("RetentionPeriod", "0000d", True),
        ("RetentionPeriod", "forever", True),
        ("RetentionPeriod", "1d\n", False),
        ("SecretRef", "secret://env/A/../key", True),
        ("SecretRef", "secret://env/key\n", False),
        ("ArtifactType", "List[List[Text]]", True),
        ("ArtifactType", "List[Text]]", True),
        ("ArtifactType", "Text\n", False),
        ("ArtifactType", "Ｔext", False),
    ],
)
def test_ir_remaining_scalar_patterns_match_schema_consumers(
    scalar: str, value: str, valid: bool
) -> None:
    import re

    adapter = SCALAR_ADAPTERS[scalar]
    pattern = cast("str", adapter.json_schema()["pattern"])

    if valid:
        parsed = adapter.validate_python(value)
        assert parsed == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)

    assert (re.search(pattern, value) is not None) is valid


def test_ir_remaining_patterns_are_used_at_each_schema_location() -> None:
    schema = cast("dict[str, object]", json.loads(SCHEMA_PATH.read_text()))
    definitions = cast("dict[str, dict[str, object]]", schema["$defs"])
    properties = cast("dict[str, dict[str, object]]", schema["properties"])
    contracts = properties["type_contracts"]
    parent = cast("list[dict[str, object]]", definitions["typeContract"]["oneOf"])[1]
    parent_properties = cast("dict[str, dict[str, object]]", parent["properties"])
    parent_alternatives = cast(
        "list[dict[str, object]]", parent_properties["parent"]["oneOf"]
    )
    expected = TypeAdapter(TypeContractName).json_schema()["pattern"]

    patterns = [
        cast("dict[str, object]", contracts["propertyNames"])["pattern"],
        parent_alternatives[1]["pattern"],
    ]

    assert patterns == [expected, expected]
    assert (
        properties["ir_version"]["pattern"]
        == TypeAdapter(IrVersion).json_schema()["pattern"]
    )
    for name, scalar in [
        ("retentionPeriod", "RetentionPeriod"),
        ("secretRef", "SecretRef"),
        ("artifactType", "ArtifactType"),
    ]:
        assert (
            definitions[name]["pattern"]
            == SCALAR_ADAPTERS[scalar].json_schema()["pattern"]
        )


def declaration_schema(model: type[IrRecord]) -> dict[str, object]:
    from tests.ir.support import schema_definition

    name = model.__name__
    direct = {
        "IrSourceLocation": "sourceLocation",
        "InputRef": "inputRef",
        "OutputDecl": "outputDecl",
        "TypeBehavior": "typeBehavior",
        "Retention": "retention",
        "RuntimeSection": "runtimeSection",
        "TargetConfig": "targetConfig",
    }
    if name in direct:
        schema = cast("dict[str, object]", schema_definition(direct[name]))
        if name == "TargetConfig":
            properties = cast("dict[str, dict[str, object]]", schema["properties"])
            key = properties["key"]
            key["anyOf"] = key.pop("oneOf")
        return schema
    if name in ("BuiltinTypeContract", "CustomTypeContract"):
        contract = cast("dict[str, object]", schema_definition("typeContract"))
        variants = cast("list[dict[str, object]]", contract["oneOf"])
        expected = variants[0 if name == "BuiltinTypeContract" else 1]
        if name == "CustomTypeContract":
            properties = cast("dict[str, dict[str, object]]", expected["properties"])
            parent = properties["parent"]
            parent["anyOf"] = list(reversed(cast("list[object]", parent.pop("oneOf"))))
        return expected
    if name in ("ParameterTarget", "OutputTarget"):
        target = cast("dict[str, object]", schema_definition("targetConfig"))
        properties = cast("dict[str, dict[str, object]]", target["properties"])
        variants = cast("list[dict[str, object]]", properties["key"]["oneOf"])
        return variants[0 if name == "ParameterTarget" else 1]
    if name in ("Concurrency", "ProviderRetry"):
        runtime = cast("dict[str, object]", schema_definition("runtimeSection"))
        properties = cast("dict[str, dict[str, object]]", runtime["properties"])
        return properties["concurrency" if name == "Concurrency" else "provider_retry"]
    if name == "SecretsSection":
        document = cast("dict[str, object]", json.loads(SCHEMA_PATH.read_text()))
        return cast("dict[str, dict[str, object]]", document["properties"])["secrets"]
    if name == "SubgraphOutput":
        subgraph = cast("dict[str, object]", schema_definition("subgraph"))
        properties = cast("dict[str, dict[str, object]]", subgraph["properties"])
        return cast("dict[str, object]", properties["outputs"]["additionalProperties"])
    trigger = cast("dict[str, object]", schema_definition("trigger"))
    properties = cast("dict[str, dict[str, object]]", trigger["properties"])
    if name == "WebhookAuth":
        return properties["auth"]
    kind = cast("str", MINIMAL[model]["kind"])
    variants = cast("list[dict[str, dict[str, object]]]", trigger["allOf"])
    for variant in variants:
        condition = cast("dict[str, dict[str, object]]", variant["if"]["properties"])
        if condition["kind"]["const"] != kind:
            continue
        then = variant["then"]
        names = cast("dict[str, object]", then["propertyNames"])
        alternatives = cast("list[dict[str, list[str]]]", names["anyOf"])
        fields = alternatives[0]["enum"]
        return {
            "type": "object",
            "required": ["kind", *cast("list[str]", then.get("required", []))],
            "properties": {
                field: {"type": "string", "const": kind}
                if field == "kind"
                else properties[field]
                for field in fields
            },
            "patternProperties": {"^x-": {}},
            "additionalProperties": False,
        }
    raise AssertionError("the trigger variant is in the handwritten schema")


@pytest.mark.parametrize("model", list(MINIMAL))
@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_ir_declaration_schemas_match_handwritten_fragments(
    model: type[IrRecord], mode: str
) -> None:
    from typing import Literal

    from tests.ir.test_ir_models_match_schema_definitions import (
        inline_known_references,
        normalised,
    )

    expected = declaration_schema(model)

    generated = model.model_json_schema(
        mode=cast("Literal['validation', 'serialization']", mode)
    )
    definitions = cast("dict[str, object]", generated.pop("$defs", {}))

    assert normalised(inline_known_references(generated, definitions)) == normalised(
        inline_known_references(expected, {})
    )
