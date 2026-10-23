import json
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from jsonschema import Draft202012Validator
from referencing import Registry

from tests.ir.support import ROOT

ARTIFACTS: Path = ROOT / "knowledge/specs/artifacts"
SCHEMA_HASH: str = "e924b9a44c0825f0b2a43b0291d1f98c4851425a50e291914dd85ba096ac6eae"
DESCRIPTOR_HASH: str = (
    "198db78bc94c844acab7f61eeb610845f65ea9e93217af566ed5c17246ab0f71"
)
VECTOR_HASH: str = "8ed41a4c96132ee20a12ad70ddf3ef14e4c482dffa7d9a4ceb33d000018bb3ff"


class SchemaValidator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


class SchemaClass(Protocol):
    def check_schema(self, schema: object) -> None: ...


def fixture_bytes(value: object) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    segments = cast("list[str | dict[str, str | int]]", value)
    return "".join(
        segment
        if isinstance(segment, str)
        else cast("str", segment["unit"]) * cast("int", segment["count"])
        for segment in segments
    ).encode("utf-8")


def test_structured_output_contract_files_are_consistent() -> None:
    schema_path = ARTIFACTS / "StructuredOutput.schema.json"
    descriptor_path = ARTIFACTS / "StructuredOutput.revision-1.json"
    expected_descriptor: dict[str, object] = {
        "name": "StructuredOutput",
        "builtin_revision": "1",
        "schema": "urn:kora:artifact:StructuredOutput",
        "parent": None,
        "behavior": {
            "validation": "kora.structured-output-validation.v1",
            "digest_fields": "kora.json-payload.v1",
            "summary": "kora.json-payload.v1",
        },
        "validation": {
            "max_raw_json_bytes": 1_048_576,
            "max_canonical_json_bytes": 1_048_576,
            "max_json_depth": 16,
            "max_integer_bits": 14_000,
        },
        "canonicalization": "kora.structured-json.v1",
        "compatibility": {
            "execution_semantics": "kora.execution.v1",
            "checker_view": "kora.structured-output-view.v1",
        },
        "digest_selection": [""],
        "schema_sha256": SCHEMA_HASH,
    }
    assert schema_path.is_file()
    assert descriptor_path.is_file()
    schema_bytes = schema_path.read_bytes()
    descriptor_bytes = descriptor_path.read_bytes()
    schema = cast("dict[str, object]", json.loads(schema_bytes))
    descriptor: object = json.loads(descriptor_bytes)
    roots = ["object", "array", "string", "number", "boolean", "null"]
    examples: list[object] = [
        json.loads(
            (ARTIFACTS / "examples/structured-output" / f"{name}.json").read_bytes()
        )
        for name in roots
    ]
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )

    cast("SchemaClass", Draft202012Validator).check_schema(schema)
    accepted = [validator.is_valid(value) for value in examples]

    assert sha256(schema_bytes).hexdigest() == SCHEMA_HASH
    assert sha256(descriptor_bytes).hexdigest() == DESCRIPTOR_HASH
    assert descriptor == expected_descriptor
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == expected_descriptor["schema"]
    assert schema["type"] == roots
    assert [type(value) for value in examples] == [
        dict,
        list,
        str,
        float,
        bool,
        type(None),
    ]
    assert all(accepted)


def test_structured_output_contract_vectors_are_immutable() -> None:
    vector_path = ARTIFACTS / "StructuredOutput.vectors.json"
    assert vector_path.is_file()
    vector_bytes = vector_path.read_bytes()
    vectors = cast("dict[str, object]", json.loads(vector_bytes))
    canonical_cases = cast("list[dict[str, object]]", vectors["canonical_cases"])
    structural_cases = cast("list[dict[str, object]]", vectors["structural_cases"])
    schema = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "StructuredOutput.schema.json").read_bytes()),
    )
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )

    canonical_results = [
        (
            fixture_bytes(case["canonical"]),
            len(fixture_bytes(case["raw"])),
            case,
        )
        for case in canonical_cases
    ]
    structural_results = [
        (
            cast(
                "SchemaValidator",
                Draft202012Validator(
                    cast("dict[str, object]", case["schema"]), registry=Registry()
                ),
            ).is_valid(case["value"]),
            case["accepted"],
        )
        for case in structural_cases
    ]

    assert sha256(vector_bytes).hexdigest() == VECTOR_HASH
    assert vectors["contract"] == "urn:kora:artifact:StructuredOutput"
    assert vectors["builtin_revision"] == "1"
    assert vectors["canonicalization"] == "kora.structured-json.v1"
    assert {case["root_kind"] for case in canonical_cases} == {
        "object",
        "array",
        "string",
        "number",
        "boolean",
        "null",
    }
    assert len({case["name"] for case in canonical_cases}) == len(canonical_cases)
    for wire, raw_size, case in canonical_results:
        assert len(wire) == case["canonical_bytes"]
        assert raw_size == case["raw_bytes"]
        assert sha256(wire).hexdigest() == case["canonical_sha256"]
        assert validator.is_valid(json.loads(wire))
    assert all(actual is expected for actual, expected in structural_results)
