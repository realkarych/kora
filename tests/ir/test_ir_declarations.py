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
    parent_properties = cast(
        "dict[str, dict[str, object]]", definitions["CustomTypeContract"]["properties"]
    )
    parent_alternatives = cast(
        "list[dict[str, object]]", parent_properties["parent"]["anyOf"]
    )
    expected = TypeAdapter(TypeContractName).json_schema()["pattern"]

    patterns = [
        cast("dict[str, object]", contracts["propertyNames"])["pattern"],
        parent_alternatives[0]["pattern"],
    ]

    assert patterns == [expected, expected]
    assert (
        properties["ir_version"]["pattern"]
        == TypeAdapter(IrVersion).json_schema()["pattern"]
    )
    retention = cast(
        "dict[str, dict[str, object]]", definitions["Retention"]["properties"]
    )
    aliases = cast(
        "dict[str, dict[str, object]]", definitions["SecretsSection"]["properties"]
    )
    output = cast(
        "dict[str, dict[str, object]]", definitions["OutputDecl"]["properties"]
    )
    for field, scalar in (
        (retention["transcripts"], "RetentionPeriod"),
        (
            cast("dict[str, object]", aliases["aliases"]["additionalProperties"]),
            "SecretRef",
        ),
        (output["type"], "ArtifactType"),
    ):
        assert field["pattern"] == SCALAR_ADAPTERS[scalar].json_schema()["pattern"]


def declaration_schema(model: type[IrRecord]) -> dict[str, object]:
    from tests.ir.support import schema_definition

    return cast("dict[str, object]", schema_definition(model.__name__))


@pytest.mark.parametrize("model", list(MINIMAL))
@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_ir_declaration_schemas_match_generated_definitions(
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
