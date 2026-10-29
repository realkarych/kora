import json
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from jsonschema import Draft202012Validator
from referencing import Registry

from tests.ir.support import ROOT

ARTIFACTS: Path = ROOT / "knowledge/specs/artifacts"
SCHEMA_HASH: str = "8444eeb4d4688b6d059dd7221689dd4c824084c88ff189ee3fbb848153c1f8d4"
DESCRIPTOR_HASH: str = (
    "855a0d3a5a3df6dc2c6405b7a306562b1f60809714989cc6b19fd70b5e682a12"
)
VECTOR_HASH: str = "5153f381d0c6fa5208d0ed7e1bf765249120ea2db964a8f79e0a67925b5a4849"


class SchemaValidator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


class SchemaClass(Protocol):
    def check_schema(self, schema: object) -> None: ...


def test_command_result_contract_files_are_consistent() -> None:
    schema_bytes = (ARTIFACTS / "CommandResult.schema.json").read_bytes()
    descriptor_bytes = (ARTIFACTS / "CommandResult.revision-1.json").read_bytes()
    schema = cast("dict[str, object]", json.loads(schema_bytes))
    descriptor: object = json.loads(descriptor_bytes)
    expected_descriptor: dict[str, object] = {
        "name": "CommandResult",
        "builtin_revision": "1",
        "schema": "urn:kora:artifact:CommandResult",
        "parent": None,
        "behavior": {
            "validation": "kora.command-result-validation.v1",
            "digest_fields": "kora.json-payload.v1",
            "summary": "kora.json-payload.v1",
        },
        "validation": {
            "max_raw_json_bytes": 1_048_576,
            "max_canonical_json_bytes": 1_048_576,
            "max_json_depth": 16,
            "max_count": 9_223_372_036_854_775_807,
            "max_capture_bytes": 67_108_864,
            "max_tail_bytes": 4_096,
            "max_resources": 64,
            "max_resource_bytes": 262_144,
            "max_resource_name_utf8_bytes": 4_096,
            "max_resource_component_utf8_bytes": 255,
            "max_protected_paths": 64,
            "max_protected_path_characters": 4_096,
            "max_protected_path_segments": 32,
            "max_protected_path_pieces": 8_192,
            "max_protected_path_utf8_bytes": 65_536,
            "max_protected_path_non_ascii_characters": 16_384,
            "max_repo_utf8_bytes": 4_096,
            "max_revision_characters": 255,
        },
        "canonicalization": "kora.command-result-json.v1",
        "digest_selection": [""],
        "schema_sha256": SCHEMA_HASH,
    }
    valid_paths = sorted((ARTIFACTS / "examples/command-result/valid").glob("*.json"))
    invalid_paths = sorted(
        (ARTIFACTS / "examples/command-result/invalid").glob("*.json")
    )
    valid_values: list[object] = [json.loads(path.read_bytes()) for path in valid_paths]
    invalid_values: list[object] = [
        json.loads(path.read_bytes()) for path in invalid_paths
    ]
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )

    cast("SchemaClass", Draft202012Validator).check_schema(schema)
    accepted_valid = [validator.is_valid(value) for value in valid_values]
    accepted_invalid = [validator.is_valid(value) for value in invalid_values]

    assert sha256(schema_bytes).hexdigest() == SCHEMA_HASH
    assert sha256(descriptor_bytes).hexdigest() == DESCRIPTOR_HASH
    assert descriptor == expected_descriptor
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == expected_descriptor["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert len(valid_paths) == 11
    assert len(invalid_paths) == 11
    assert all(accepted_valid)
    assert not any(accepted_invalid)


def test_command_result_contract_vectors_are_immutable() -> None:
    vector_bytes = (ARTIFACTS / "CommandResult.vectors.json").read_bytes()
    vectors = cast("dict[str, object]", json.loads(vector_bytes))
    canonical_cases = cast("list[dict[str, object]]", vectors["canonical_cases"])
    invalid_fixtures = cast("list[str]", vectors["structural_invalid_fixtures"])
    valid_paths = {
        path.relative_to(ARTIFACTS).as_posix()
        for path in (ARTIFACTS / "examples/command-result/valid").glob("*.json")
    }
    invalid_paths = {
        path.relative_to(ARTIFACTS).as_posix()
        for path in (ARTIFACTS / "examples/command-result/invalid").glob("*.json")
    }

    canonical_results = [
        (
            cast("str", case["canonical"]).encode("utf-8"),
            (ARTIFACTS / cast("str", case["fixture"])).read_bytes(),
            case,
        )
        for case in canonical_cases
    ]

    assert sha256(vector_bytes).hexdigest() == VECTOR_HASH
    assert vectors["contract"] == "urn:kora:artifact:CommandResult"
    assert vectors["builtin_revision"] == "1"
    assert vectors["canonicalization"] == "kora.command-result-json.v1"
    assert len({case["name"] for case in canonical_cases}) == len(canonical_cases)
    assert {case["fixture"] for case in canonical_cases} == valid_paths
    assert len(canonical_cases) == len(valid_paths)
    assert set(invalid_fixtures) == invalid_paths
    assert len(invalid_fixtures) == len(invalid_paths)
    for wire, fixture, case in canonical_results:
        assert wire == fixture
        assert len(wire) == case["canonical_bytes"]
        assert sha256(wire).hexdigest() == case["canonical_sha256"]
        assert case["selected_pointers"] == [""]
        assert case["selected_sha256"] == case["canonical_sha256"]
