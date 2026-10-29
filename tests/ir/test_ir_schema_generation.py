import gc
import json
import os
import re
import subprocess
import sys
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from pydantic.json_schema import GenerateJsonSchema

from kora.ir import GraphDocument, HumanInputConfig, export_ir
from tests.ir.schema_document_cases import boundary_cases
from tests.ir.support import ROOT, SCHEMA_PATH, raw_error_forms

CORPUS = (
    SCHEMA_PATH.parents[1]
    / "research"
    / "ir-document-envelope-checker-corpus-2026-10-23.json"
)


def schema_objects(value: object) -> Iterator[dict[str, object]]:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            record = cast("dict[str, object]", item)
            yield record
            pending.extend(record.values())
        elif isinstance(item, list):
            pending.extend(cast("list[object]", item))


def resolve_pointer(document: dict[str, object], reference: str) -> object:
    assert reference.startswith("#/")
    result: object = document
    for part in reference[2:].split("/"):
        result = pointer_member(result, part.replace("~1", "/").replace("~0", "~"))
    return result


def pointer_member(value: object, name: str) -> object:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)[name]


def test_ir_schema_is_generated_and_current() -> None:
    from kora.ir.schema import generate_ir_schema, render_ir_schema

    generated = generate_ir_schema()
    rendered = render_ir_schema()

    assert generated["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert generated["$id"] == "https://kora.dev/schemas/ir/1.0.json"
    assert json.loads(rendered) == generated
    assert rendered == json.dumps(generated, indent=2, ensure_ascii=False) + "\n"
    assert SCHEMA_PATH.read_bytes() == rendered.encode("utf-8")


def test_ir_document_models_match_schema() -> None:
    from kora.ir.schema import generate_ir_schema

    expected = generate_ir_schema()

    actual = GraphDocument.model_json_schema(by_alias=True, mode="serialization")

    assert actual == {
        name: value
        for name, value in expected.items()
        if name not in {"$id", "$schema"}
    }
    for record in schema_objects(expected):
        if "$ref" in record:
            assert resolve_pointer(expected, cast("str", record["$ref"]))
        if "discriminator" in record:
            discriminator = cast("dict[str, object]", record["discriminator"])
            for reference in cast("dict[str, str]", discriminator["mapping"]).values():
                assert resolve_pointer(expected, reference)


def test_ir_repository_schema_fragment_links_resolve() -> None:
    from kora.ir.schema import generate_ir_schema

    schema = generate_ir_schema()
    references = [
        reference
        for directory in (ROOT / "docs", ROOT / "knowledge")
        for path in directory.rglob("*.md")
        for reference in re.findall(
            r"ir\.schema\.json(\#/\$defs/[^)\s]+)", path.read_text()
        )
    ]

    resolved = [resolve_pointer(schema, reference) for reference in references]

    assert len(resolved) >= 13
    assert all(isinstance(value, dict) for value in resolved)


def test_ir_checker_inputs_keep_actual_modes_and_raw_cases(tmp_path: Path) -> None:
    from tests.ir.schema_document_cases import prepare_checker_inputs

    prepare_checker_inputs(tmp_path)

    for mode in ("validation", "serialization"):
        actual = json.loads((tmp_path / f"{mode}.schema.json").read_text())
        assert actual == {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **GraphDocument.model_json_schema(by_alias=True, mode=mode),
        }
    cases = json.loads((tmp_path / "corpus.json").read_text())["cases"]
    assert len(cases) == 176
    assert len({case["name"] for case in cases}) == 176
    assert (tmp_path / "committed.schema.json").read_bytes() == SCHEMA_PATH.read_bytes()


def test_ir_schema_annotations_preserve_approval_defaults() -> None:
    from kora.ir.schema import generate_ir_schema

    expected_form = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "comments": {"type": "string"},
        },
        "required": ["decision"],
        "additionalProperties": False,
    }
    document = {"channel": "cli", "timeout": "0s", "on_expiry": "fail"}

    schema = generate_ir_schema()
    parsed = HumanInputConfig.model_validate(document)
    definitions = cast("dict[str, dict[str, object]]", schema["$defs"])
    human = cast(
        "dict[str, dict[str, object]]", definitions["HumanInputConfig"]["properties"]
    )
    policy = cast(
        "dict[str, dict[str, object]]", definitions["ToolPolicy"]["properties"]
    )
    defaults = [
        record["default"] for record in schema_objects(schema) if "default" in record
    ]

    assert human["form"]["default"] == expected_form
    assert export_ir(parsed) == document
    assert len(defaults) == 3
    assert defaults.count([]) == 2
    assert expected_form in defaults
    assert policy["allow"]["default"] == policy["deny"]["default"] == []


def test_ir_schema_keeps_required_fields_and_exclusive_choices() -> None:
    from kora.ir.schema import generate_ir_schema

    schema = generate_ir_schema()
    definitions = cast("dict[str, dict[str, object]]", schema["$defs"])
    root = cast("dict[str, object]", schema["properties"])

    assert set(cast("list[str]", schema["required"])) == {
        "ir_version",
        "type_contracts",
        "graph",
        "runtime",
    }
    assert "$schema" in root
    assert "schema_" not in root
    assert schema["additionalProperties"] is True
    assert definitions["Provenance"]["additionalProperties"] is True
    assert sum(record.get("type") == "null" for record in schema_objects(schema)) == 1
    assert definitions["AgentConfig"]["required"] == ["harness", "prompt", "workspace"]
    assert definitions["HumanInputConfig"]["required"] == [
        "channel",
        "timeout",
        "on_expiry",
    ]
    assert definitions["WaitEventConfig"]["required"] == ["timeout", "on_expiry"]
    assert definitions["WaitEventConfig"]["oneOf"] == [
        {"required": ["event"]},
        {"required": ["poll"]},
    ]
    assert definitions["TimerConfig"]["oneOf"] == [
        {"required": ["duration"]},
        {"required": ["at"]},
    ]
    assert "required" not in definitions["TimerConfig"]
    assert "required" not in definitions["Provenance"]
    assert definitions["Graph"]["required"] == ["nodes", "id", "namespace"]
    assert definitions["Subgraph"]["required"] == ["nodes"]
    assert (
        definitions["InputRef"]["required"]
        == definitions["OutputDecl"]["required"]
        == ["type"]
    )


def test_ir_schema_fixes_role_output_declarations() -> None:
    from kora.ir.schema import generate_ir_schema

    declaration = {"$ref": "#/$defs/OutputDecl"}
    roles = (
        ("AgentNode", "session", "AgentSession", False),
        ("CommandNode", "result", "CommandResult", False),
        ("HumanInputNode", "response", "StructuredOutput", True),
        ("WaitEventNode", "event", "StructuredOutput", True),
    )

    schema = generate_ir_schema()
    definitions = cast("dict[str, dict[str, object]]", schema["$defs"])

    for name, role, type_name, exclusive in roles:
        fields = cast("dict[str, dict[str, object]]", definitions[name]["properties"])
        outputs = fields["outputs"]
        declared = cast("dict[str, dict[str, object]]", outputs["properties"])[role]
        assert outputs["required"] == [role]
        assert declared["allOf"] == [
            declaration,
            {"properties": {"type": {"const": type_name}}},
        ]
        if exclusive:
            assert outputs["additionalProperties"] is False
        else:
            assert outputs["additionalProperties"] == {
                "allOf": [
                    declaration,
                    {"properties": {"type": {"not": {"const": type_name}}}},
                ]
            }
        assert set(cast("list[str]", definitions[name]["required"])) == {
            "kind",
            "outputs",
            "config",
        }


def test_timer_timestamp_schema_keeps_portable_pattern_and_format() -> None:
    from kora.ir.schema import generate_ir_schema

    expected = (
        r"^(?!0000)[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])[Tt]"
        r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?"
        r"([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])(?![\s\S])"
    )

    schema = generate_ir_schema()
    definitions = cast("dict[str, dict[str, object]]", schema["$defs"])
    timer = cast(
        "dict[str, dict[str, object]]", definitions["TimerConfig"]["properties"]
    )

    assert timer["at"]["format"] == "date-time"
    assert timer["at"]["pattern"] == expected


def test_ir_schema_generation_is_deterministic() -> None:
    from kora.ir.schema import generate_ir_schema, render_ir_schema

    first = generate_ir_schema()
    rendered = render_ir_schema()
    first.clear()

    assert render_ir_schema() == rendered
    assert generate_ir_schema() == json.loads(rendered)
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")


@pytest.mark.parametrize("retained", [True, False])
def test_ir_schema_generator_inventory_observes_real_retention(retained: bool) -> None:
    from tests.measurements.ir_schema_lifetime import live_schema_generators

    gc.collect()
    baseline = live_schema_generators()
    generator = GenerateJsonSchema()
    reference = weakref.ref(generator)
    if not retained:
        del generator

    gc.collect()
    observed = live_schema_generators()

    assert observed == baseline + int(retained)
    assert (reference() is not None) is retained


def test_ir_schema_command_has_only_schema_stdout_without_artifact_access(
    tmp_path: Path,
) -> None:
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from kora.ir.schema import render_ir_schema; "
            'print(render_ir_schema(), end="")',
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout == SCHEMA_PATH.read_bytes()


def test_ir_independent_document_schema_corpus() -> None:
    corpus = cast("dict[str, object]", json.loads(CORPUS.read_text()))
    cases = cast("list[dict[str, object]]", corpus["cases"])
    cases.extend(
        {"name": name, "document": value, "valid": expected}
        for name, value, expected in boundary_cases()
    )

    outcomes: dict[str, bool] = {}
    for case in cases:
        try:
            GraphDocument.model_validate(case["document"])
        except ValidationError:
            outcomes[cast("str", case["name"])] = False
        else:
            outcomes[cast("str", case["name"])] = True

    assert outcomes == {cast("str", case["name"]): case["valid"] for case in cases}
    assert len(outcomes) == len(cases) >= 112


def test_ir_schema_nullability_aliases_and_named_maps() -> None:
    from kora.ir.schema import generate_ir_schema

    schema = generate_ir_schema()
    definitions = cast("dict[str, dict[str, object]]", schema["$defs"])

    for definition in definitions.values():
        properties = cast(
            "dict[str, dict[str, object]]", definition.get("properties", {})
        )
        assert not ({"extensions", "schema_", "from_", "else_"} & properties.keys())
        assert "unknown_root_key_count" not in properties
    custom = cast(
        "dict[str, dict[str, object]]", definitions["CustomTypeContract"]["properties"]
    )
    assert set(cast("list[str]", definitions["CustomTypeContract"]["required"])) == {
        "schema",
        "parent",
        "behavior",
    }
    assert custom["parent"]["anyOf"] == [
        {
            "pattern": r"^([a-z][a-z0-9_]*/)?[A-Z][A-Za-z0-9]*(?![\s\S])",
            "type": "string",
        },
        {"type": "null"},
    ]
    assert custom["schema"]["properties"] == {
        "$schema": {"const": "https://json-schema.org/draft/2020-12/schema"}
    }
    node_pattern = r"^[a-z][a-z0-9_]{0,63}(?![\s\S])"
    for model, fields in (
        ("Graph", ("nodes", "subgraphs")),
        ("Subgraph", ("nodes", "inputs", "outputs")),
        ("TimerNode", ("inputs", "outputs")),
    ):
        properties = cast(
            "dict[str, dict[str, object]]", definitions[model]["properties"]
        )
        for name in fields:
            assert properties[name]["propertyNames"] == {
                "pattern": node_pattern,
                "type": "string",
            }
            assert "patternProperties" not in properties[name]
    agent = cast(
        "dict[str, dict[str, object]]", definitions["AgentConfig"]["properties"]
    )
    assert "propertyNames" not in agent["mcp_servers"]
    assert agent["env"]["propertyNames"] == {
        "pattern": r"^[A-Z][A-Z0-9_]{0,63}(?![\s\S])",
        "type": "string",
    }


def test_ir_ignored_root_boundary_never_traverses_values() -> None:
    from tests.ir.document_cases import document

    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    value = {
        **document(),
        "ignored_cycle": cycle,
        "ignored_large": "a" * 3_145_728,
        "unknown_root_key_count": 9000,
        "_unknown_root_key_count": 9000,
    }

    parsed = GraphDocument.model_validate(value)

    assert parsed.unknown_root_key_count == 4
    assert export_ir(parsed) == document()


def test_ir_schema_measurement_inventory_preserves_all_actions() -> None:
    from tests.measurements.ir_measurement_evidence import (
        expected_evidence,
        structured_output_evidence,
    )
    from tests.measurements.ir_schema_workload import schema_fixture_manifest
    from tests.measurements.test_ir_models_cost import WORKLOADS

    expected = expected_evidence()
    previous = expected.without(structured_output_evidence())
    manifest = cast(
        "dict[str, dict[str, object]]", json.loads(schema_fixture_manifest())
    )
    workloads = {
        name: row for name, row in WORKLOADS.items() if name.startswith("schema_")
    }

    assert set(workloads) == {
        "schema_validation",
        "schema_serialization",
        "schema_render",
        "schema_render_utf8",
    }
    assert len(workloads) == 4
    assert sum(row[3] for row in workloads.values()) == 120
    assert (
        len(expected.nodes),
        len(expected.gates),
        sum(expected.samples.values()),
    ) == (366, 1271, 11054)
    assert (
        len(previous.nodes),
        len(previous.gates),
        sum(previous.samples.values()),
    ) == (359, 1227, 10668)
    for mode in ("validation", "serialization", "render_utf8"):
        assert manifest[mode]["definitions"] == 53
        assert manifest[mode]["references"] == 110
        assert cast("int", manifest[mode]["bytes"]) > 65_000


def test_ir_document_late_invalid_open_data_errors_exclude_input_content() -> None:
    private_content = "private-untrusted-content"
    values: list[object] = [{"data": index} for index in range(10_000)]
    values[-1] = {private_content: object()}
    value = {
        "ir_version": "1.0",
        "type_contracts": {},
        "graph": {
            "id": "graph",
            "namespace": "example",
            "nodes": {
                "agent": {
                    "kind": "agent",
                    "outputs": {"session": {"type": "AgentSession"}},
                    "config": {
                        "harness": "claude",
                        "prompt": private_content,
                        "workspace": {"repo": "file:///private/source"},
                        "output_schema": {"data": values},
                    },
                }
            },
        },
        "runtime": {"runtime": "local", "vcs": "git"},
    }

    with pytest.raises(ValidationError) as captured:
        GraphDocument.model_validate(value)
    rendered = raw_error_forms(captured.value)

    assert private_content not in rendered
    assert "file:///private/source" not in rendered
