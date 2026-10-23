import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import cast


def boundary_cases() -> list[tuple[str, dict[str, object], bool]]:
    baseline: dict[str, object] = {
        "ir_version": "1.0",
        "type_contracts": {},
        "graph": {
            "id": "graph",
            "namespace": "example",
            "nodes": {"timer": {"kind": "timer", "config": {"duration": "0s"}}},
        },
        "runtime": {"runtime": "local", "vcs": "git"},
    }
    values: list[tuple[str, dict[str, object], bool]] = [
        (
            "forged_nonwire_counters",
            {
                **baseline,
                "unknown_root_key_count": 9000,
                "_unknown_root_key_count": 9000,
            },
            True,
        ),
        ("raw_schema_label", {**baseline, "$schema": "file:///private/secret"}, True),
        ("internal_schema_label_ignored", {**baseline, "schema_": False}, True),
        (
            "provenance_operational_strings",
            {
                **baseline,
                "meta": {
                    "hooks": {"on_start": "import:untrusted"},
                    "subagents": [{"prompt": "file:///private"}],
                    "mcp_servers": {"remote": "https://not-resolved.example"},
                },
            },
            True,
        ),
        (
            "provenance_known_field_refusal",
            {**baseline, "meta": {"compiler": False}},
            False,
        ),
        (
            "provenance_schema_keywords_data",
            {
                **baseline,
                "meta": {
                    "schema": {
                        "required": False,
                        "outputs": [None],
                        "config": {"kind": False},
                    }
                },
            },
            True,
        ),
        ("root_null_refused", {**baseline, "graph": None}, False),
        ("root_extension_data", {**baseline, "x-meta": {"schema": False}}, True),
        (
            "root_required_contracts",
            {
                name: value
                for name, value in baseline.items()
                if name != "type_contracts"
            },
            False,
        ),
    ]
    for label in (
        "/private/source.py",
        "file:///private/source.py",
        "https://not-resolved.example/source",
    ):
        values.append(
            (
                "source_" + label,
                {
                    **baseline,
                    "graph": {
                        "id": "graph",
                        "namespace": "example",
                        "nodes": {
                            "timer": {
                                "kind": "timer",
                                "source": {"file": label, "line": 1},
                                "config": {"duration": "0s"},
                            }
                        },
                    },
                },
                True,
            )
        )
    for reference in ("https://not-resolved.example/schema", "file:///private/schema"):
        values.append(
            (
                "schema_reference_" + reference,
                {
                    **baseline,
                    "type_contracts": {
                        "Text": {
                            "schema": {
                                "$schema": "https://json-schema.org/draft/2020-12/schema",
                                "$ref": reference,
                                "properties": {
                                    "source": False,
                                    "config": True,
                                    "outputs": {"type": "null"},
                                },
                            },
                            "parent": None,
                            "behavior": {
                                "validation": "import:untrusted",
                                "digest_fields": "file:///private/key",
                                "summary": "https://not-resolved.example/summary",
                            },
                        }
                    },
                },
                True,
            )
        )
    for field, value in (
        ("hooks", {"on_start": "import:untrusted"}),
        (
            "subagents",
            [{"name": "", "description": "", "prompt": "file:///private/key"}],
        ),
        (
            "mcp_servers",
            {"remote": {"type": "http", "url": "https://not-resolved.example"}},
        ),
    ):
        values.append(
            (
                "agent_" + field,
                {
                    **baseline,
                    "graph": {
                        "id": "graph",
                        "namespace": "example",
                        "nodes": {
                            "agent": {
                                "kind": "agent",
                                "outputs": {"session": {"type": "AgentSession"}},
                                "config": {
                                    "harness": "claude",
                                    "prompt": "",
                                    "workspace": {"repo": ""},
                                    field: value,
                                },
                            }
                        },
                    },
                },
                True,
            )
        )
    for timestamp, valid in (
        ("0001-01-01T00:00:00Z", True),
        ("9999-12-31t23:59:59z", True),
        ("2000-02-29T01:02:03.1+23:59", True),
        ("2024-02-29t23:59:59.120000-00:00", True),
        ("2024-01-01T00:00:00.123456Z", True),
        ("0000-01-01T00:00:00Z", False),
        ("1900-02-29T00:00:00Z", False),
        ("2024-04-31T00:00:00Z", False),
        ("2024-01-01T24:00:00Z", False),
        ("2024-01-01T00:00:60Z", False),
        ("2024-01-01T00:00:00+24:00", False),
        ("2024-01-01T00:00:00.1234567Z", False),
        ("2024-01-01T00:00:00,1Z", False),
        ("2024-01-01T00:00:00", False),
        ("2024-01-01T00:00:00Z\n", False),
        ("２０２４-01-01T00:00:00Z", False),
    ):
        values.append(
            (
                "timestamp_" + repr(timestamp),
                {
                    **baseline,
                    "graph": {
                        **cast("dict[str, object]", baseline["graph"]),
                        "nodes": {
                            "timer": {"kind": "timer", "config": {"at": timestamp}}
                        },
                    },
                },
                valid,
            )
        )
    for label, config, valid in (
        ("zero", {"duration": "0s"}, True),
        ("leading_zero", {"duration": "001ms"}, True),
        ("final_newline", {"duration": "1s\n"}, False),
        ("null", {"duration": None}, False),
        ("numeric", {"at": 0}, False),
        ("neither", {}, False),
        ("both", {"duration": "0s", "at": "2024-01-01T00:00:00Z"}, False),
    ):
        values.append(
            (
                "timer_" + label,
                {
                    **baseline,
                    "graph": {
                        **cast("dict[str, object]", baseline["graph"]),
                        "nodes": {"timer": {"kind": "timer", "config": config}},
                    },
                },
                valid,
            )
        )
    for version, valid in (("1.000", True), ("1.0\n", False)):
        values.append(
            ("version_" + repr(version), {**baseline, "ir_version": version}, valid)
        )
    for period, valid in (("0000d", True), ("forever", True), ("1d\n", False)):
        values.append(
            (
                "retention_" + repr(period),
                {
                    **baseline,
                    "runtime": {
                        "runtime": "local",
                        "vcs": "git",
                        "retention": {"transcripts": period},
                    },
                },
                valid,
            )
        )
    for reference, valid in (
        ("secret://env/A/../key", True),
        ("secret://env/key\n", False),
    ):
        values.append(
            (
                "secret_reference_" + repr(reference),
                {**baseline, "secrets": {"aliases": {"TOKEN": reference}}},
                valid,
            )
        )
    for artifact, valid in (
        ("List[Text]]", True),
        ("Text\n", False),
        ("List[List[ns/Text]]", True),
    ):
        values.append(
            (
                "artifact_" + repr(artifact),
                {
                    **baseline,
                    "graph": {
                        **cast("dict[str, object]", baseline["graph"]),
                        "nodes": {
                            "timer": {
                                "kind": "timer",
                                "config": {"duration": "0s"},
                                "outputs": {"data": {"type": artifact}},
                            }
                        },
                    },
                },
                valid,
            )
        )
    for harness, env, valid in (
        ("acp:agent", {"TOKEN": ""}, True),
        ("claude\n", {"TOKEN": ""}, False),
        ("claude", {"TOKEN\n": ""}, False),
        ("claude", {"x-token": ""}, False),
    ):
        values.append(
            (
                "agent_names_" + repr((harness, env)),
                {
                    **baseline,
                    "graph": {
                        **cast("dict[str, object]", baseline["graph"]),
                        "nodes": {
                            "agent": {
                                "kind": "agent",
                                "outputs": {"session": {"type": "AgentSession"}},
                                "config": {
                                    "harness": harness,
                                    "prompt": "",
                                    "workspace": {"repo": ""},
                                    "env": env,
                                },
                            }
                        },
                    },
                },
                valid,
            )
        )
    for sensor, valid in (("http", True), ("http\n", False)):
        values.append(
            (
                "sensor_name_" + repr(sensor),
                {
                    **baseline,
                    "graph": {
                        **cast("dict[str, object]", baseline["graph"]),
                        "nodes": {
                            "wait": {
                                "kind": "wait_event",
                                "outputs": {"event": {"type": "StructuredOutput"}},
                                "config": {
                                    "poll": {"sensor": sensor, "until": "true"},
                                    "timeout": "0s",
                                    "on_expiry": "fail",
                                },
                            }
                        },
                    },
                },
                valid,
            )
        )
    for suffix, valid in (("", True), ("\n", False)):
        values.append(
            (
                "digest_" + repr(suffix),
                {**baseline, "meta": {"source_digest": "sha256:" + "a" * 64 + suffix}},
                valid,
            )
        )
        values.append(
            (
                "input_source_" + repr(suffix),
                {
                    **baseline,
                    "graph": {
                        **cast("dict[str, object]", baseline["graph"]),
                        "nodes": {
                            "timer": {
                                "kind": "timer",
                                "config": {"duration": "0s"},
                                "inputs": {
                                    "data": {"type": "Text", "from": "$params" + suffix}
                                },
                            }
                        },
                    },
                },
                valid,
            )
        )
        values.append(
            (
                "branch_target_" + repr(suffix),
                {
                    **baseline,
                    "graph": {
                        **cast("dict[str, object]", baseline["graph"]),
                        "nodes": {
                            "branch": {
                                "kind": "branch",
                                "config": {
                                    "cases": [
                                        {
                                            "when": "true",
                                            "to": ["$fail_attempt" + suffix],
                                        }
                                    ]
                                },
                            }
                        },
                    },
                },
                valid,
            )
        )
    return [(name, deepcopy(value), expected) for name, value, expected in values]


def prepare_checker_inputs(directory: Path) -> None:
    from pydantic import ValidationError

    from kora.ir import GraphDocument
    from kora.ir.contracts import SCHEMA_DIALECT
    from kora.ir.schema import render_ir_schema
    from tests.ir.support import EXAMPLES, SCHEMA_PATH, load_yaml

    directory.mkdir(parents=True, exist_ok=True)
    committed = SCHEMA_PATH.read_bytes()
    if committed != render_ir_schema().encode("utf-8"):
        raise ValueError("the committed IR schema is stale")
    (directory / "committed.schema.json").write_bytes(committed)
    for mode in ("validation", "serialization"):
        schema = {
            "$schema": SCHEMA_DIALECT,
            **GraphDocument.model_json_schema(by_alias=True, mode=mode),
        }
        (directory / f"{mode}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        )
    corpus = cast(
        "dict[str, object]",
        json.loads(
            (
                SCHEMA_PATH.parents[1]
                / "research"
                / "ir-document-envelope-checker-corpus-2026-10-23.json"
            ).read_text()
        ),
    )
    cases = cast("list[dict[str, object]]", corpus["cases"])
    cases.extend(
        {"name": name, "document": value, "valid": valid}
        for name, value, valid in boundary_cases()
    )
    cases.extend(
        {"name": path.name, "document": load_yaml(path), "valid": True}
        for path in sorted(EXAMPLES.glob("*.ir.yaml"))
    )
    for case in cases:
        try:
            GraphDocument.model_validate(case["document"])
        except ValidationError:
            actual = False
        else:
            actual = True
        if actual is not case["valid"]:
            raise ValueError("the independent model corpus differs")
    (directory / "corpus.json").write_text(
        json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise ValueError("expected checker input directory")
    prepare_checker_inputs(Path(sys.argv[1]))
