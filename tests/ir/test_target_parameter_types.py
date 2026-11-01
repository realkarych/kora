import gc
import weakref
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import MISSING, ValidationError

from kora.cel import SchemaTypes
from kora.cel.types import Record
from kora.ir import (
    CustomTypeContract,
    export_ir,
    validate_first_slice_configuration,
    validate_known_target_parameter_types,
)
from kora.ir.json import JsonObject, JsonValue
from tests.ir.graph_link_cases import node, ref
from tests.ir.inline_schema_cases import document_for
from tests.ir.target_parameter_cases import (
    parameter_schema,
    saved_parameters,
    target_document,
)


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize(
    ("member", "valid"),
    [
        ({"type": "string"}, True),
        ({"enum": ["first", "second"]}, True),
        ({"const": "target"}, True),
        ({"type": "string", "enum": [True]}, True),
        ({"type": "integer"}, False),
        ({"type": "number"}, False),
        ({"type": "boolean"}, False),
        ({"type": "array"}, False),
        ({"type": "object"}, False),
        ({"type": ["string", "null"]}, False),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, False),
        ({"enum": ["first", None]}, False),
    ],
)
def test_known_target_parameter_types_compare_only_declared_properties(
    member: JsonValue, valid: bool, required: bool
) -> None:
    document = target_document(parameter_schema(member, required))
    before = export_ir(document)

    result = validate_known_target_parameter_types(document)

    assert result.known_target_parameter_types_valid is valid
    assert result.parameter_targets_checked == result.parameter_types_resolved == 1
    assert result.parameter_types_unresolved == 0
    assert result.schema_steps > 0
    assert len(result.diagnostics.errors) == int(not valid)
    assert export_ir(document) == before


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize(
    "wrapper", ["root_ref", "member_ref", "allOf", "saved", "builtin"]
)
def test_known_target_parameter_types_preserve_mapping_rules(
    wrapper: str, valid: bool
) -> None:
    member: JsonObject = {"type": "string" if valid else "integer"}
    schema: JsonObject = parameter_schema(member)
    contracts: dict[str, dict[str, object]] = {}
    if wrapper == "root_ref":
        schema = {
            "$defs": {"parameters": schema},
            "$ref": "#/$defs/parameters",
            "type": "boolean",
        }
    elif wrapper == "member_ref":
        schema = {
            **parameter_schema({"$ref": "#/$defs/value"}),
            "$defs": {"value": member},
        }
    elif wrapper == "allOf":
        schema = {"allOf": [schema], "type": "boolean"}
    elif wrapper == "saved":
        contracts["example/Parameters"] = saved_parameters(schema)
        schema = {"$ref": "urn:kora:artifact:example:Parameters"}
    else:
        contracts["VcsRef"] = {"builtin_revision": "1"}
        schema = parameter_schema(
            {"$ref": "urn:kora:artifact:VcsRef#/properties/branch"}
        )
    document = target_document(schema, contracts=contracts)

    result = validate_known_target_parameter_types(document)

    assert result.known_target_parameter_types_valid is (valid or wrapper == "builtin")
    assert result.parameter_types_resolved == 1
    assert result.parameter_types_unresolved == 0
    assert len(result.diagnostics.errors) == int(not valid and wrapper != "builtin")


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"type": "object"},
        {"type": "string"},
        {"properties": {}},
        {"properties": {}, "additionalProperties": False},
        {"properties": {}, "additionalProperties": {"type": "integer"}},
        {
            "properties": {},
            "patternProperties": {"target": {"type": "string"}},
            "additionalProperties": False,
        },
        {"type": ["object", "null"], "properties": {"target": {"type": "string"}}},
        parameter_schema({}),
        parameter_schema(True),
        parameter_schema(False),
        parameter_schema({"type": "null"}),
        parameter_schema({"enum": ["first", 1]}),
        parameter_schema({"type": ["string", "integer"]}),
        parameter_schema({"allOf": [{"type": "string"}, {"const": "first"}]}),
        parameter_schema({"$ref": "#/$defs/missing"}),
        parameter_schema({"$ref": "urn:kora:artifact:example:Missing"}),
        parameter_schema({"$ref": "urn:kora:artifact:StructuredOutput"}),
        parameter_schema({"$ref": "https://unavailable.invalid/schema"}),
        parameter_schema({"$ref": "/private/unavailable.json"}),
        parameter_schema({"type": "unknown"}),
        {"properties": []},
    ],
)
def test_known_target_parameter_types_leave_other_sources_unresolved(
    params: JsonObject | None,
) -> None:
    document = target_document(params)

    result = validate_known_target_parameter_types(document)

    assert not result.known_target_parameter_types_valid
    assert result.diagnostics.valid
    assert result.parameter_targets_checked == result.parameter_types_unresolved == 1
    assert result.parameter_types_resolved == 0
    assert not result.diagnostics.errors


def test_known_target_parameter_types_do_not_read_file_reference(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"type":"string"}')
    document = target_document(parameter_schema({"$ref": schema_path.as_uri()}))

    result = validate_known_target_parameter_types(document)

    assert result.parameter_types_unresolved == 1
    assert not result.known_target_parameter_types_valid
    assert not result.diagnostics.errors
    assert schema_path.read_text() == '{"type":"string"}'


@pytest.mark.parametrize(
    "target",
    [MISSING, {"key": {"from": "absent", "output": "missing", "path": "private["}}],
)
def test_known_target_parameter_types_exclude_other_target_forms(
    target: object,
) -> None:
    document = document_for(
        params={"$ref": "https://unavailable.invalid/schema"}, target=target
    )

    result = validate_known_target_parameter_types(document)

    assert result.known_target_parameter_types_valid
    assert (
        result.parameter_targets_checked
        == result.parameter_types_resolved
        == result.parameter_types_unresolved
        == result.schema_steps
        == 0
    )


def test_known_target_types_keep_first_slice_refusal() -> None:
    schema = parameter_schema({"type": "string"})
    document = target_document(
        schema,
        nodes={
            "subject": node(
                "timer", inputs={"target": ref("$params")}, **{"x-params": schema}
            )
        },
        bodies={
            "recursive": {
                "nodes": {"call": node("subgraph", config={"body": "recursive"})}
            }
        },
        triggers=[{"kind": "manual", "params": {"target": 1}}],
    )

    result = validate_known_target_parameter_types(document)
    installation = validate_first_slice_configuration(document)

    assert result.known_target_parameter_types_valid
    assert result.parameter_targets_checked == result.parameter_types_resolved == 1
    assert any(
        error.code == "IR028" and error.path == ("graph", "target")
        for error in installation.diagnostics.errors
    )


@pytest.mark.parametrize(
    "source",
    [
        MISSING,
        "graph.py",
        "/private/source",
        "https://invalid/source",
        "\ud800",
        "s" * 1025,
    ],
)
@pytest.mark.parametrize(
    "parameter",
    [
        "PRIVATE_PARAMETER",
        "https://private.invalid/name",
        "private_😀_name",
        "p" * 5000,
    ],
)
def test_known_target_parameter_types_keep_safe_location(
    source: object, parameter: str
) -> None:
    document = target_document(
        parameter_schema({"type": "integer"}, name=parameter),
        parameter=parameter,
        source=MISSING if source is MISSING else {"file": source, "line": 7},
        nodes={"other": node(source={"file": "node.py", "line": 1})},
    )

    result = validate_known_target_parameter_types(document)

    (error,) = result.diagnostics.errors
    assert error.code == "IR020"
    assert error.message == "The declared target parameter must have a string type."
    assert error.path == ("graph", "target", "key", "param")
    assert error.node is error.offset is error.hint is None
    assert parameter not in repr(result)
    if source == "graph.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 7)
    else:
        assert error.location is None


@pytest.mark.parametrize("ending", ["valid", "invalid", "unresolved"])
def test_known_target_parameter_types_release_inputs(ending: str) -> None:
    schema = parameter_schema({"type": "integer" if ending == "invalid" else "string"})
    if ending == "unresolved":
        schema = parameter_schema({"$ref": "urn:kora:artifact:example:Missing"})
    document = target_document(
        {"$ref": "urn:kora:artifact:example:Parameters"},
        contracts={"example/Parameters": saved_parameters(schema)},
        source={"file": "graph.py", "line": 7},
    )
    assert document.graph.params is not MISSING
    assert document.graph.target is not MISSING
    contract = document.type_contracts["example/Parameters"]
    assert isinstance(contract, CustomTypeContract)
    params = document.graph.params
    target = document.graph.target
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(params),
        weakref.ref(target),
        weakref.ref(target.key),
        weakref.ref(document.graph.source),
        weakref.ref(contract),
        weakref.ref(contract.schema_),
    ]
    before = export_ir(document)
    gc.collect()
    baseline = sum(
        isinstance(value, SchemaTypes | Record) for value in gc.get_objects()
    )

    result = validate_known_target_parameter_types(document)

    assert document.graph.params is params
    assert document.graph.target is target
    assert export_ir(document) == before
    assert result.known_target_parameter_types_valid is (ending == "valid")
    del document, params, target, contract
    gc.collect()
    assert all(reference() is None for reference in references)
    assert (
        sum(isinstance(value, SchemaTypes | Record) for value in gc.get_objects())
        == baseline
    )
    diagnostic_references: list[Callable[[], object | None]] = [
        weakref.ref(error) for error in result.diagnostics.errors
    ]
    diagnostic_references.extend(
        weakref.ref(error.location) for error in result.diagnostics.errors
    )
    result_reference = weakref.ref(result)
    del result
    gc.collect()
    assert all(reference() is None for reference in diagnostic_references)
    assert result_reference() is None


def test_target_parameter_structural_admission_refuses_surrogate() -> None:
    parameter = "\ud800"
    schema = parameter_schema({"type": "integer"}, name=parameter)

    with pytest.raises(ValidationError, match="string_unicode"):
        target_document(schema, parameter=parameter)
