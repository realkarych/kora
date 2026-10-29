import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from jsonschema import Draft202012Validator
from referencing import Registry

from tests.ir.support import EXAMPLES, ROOT, load_yaml

ARTIFACTS: Path = ROOT / "knowledge/specs/artifacts"
SCHEMA_HASH: str = "d7a63554c3c50b9237a118f788ffd4a0f0af262bc192c2301db568f7c902e55a"
DESCRIPTOR_HASH: str = (
    "644a0891ccd6bed032090e6efbf94f4b5c3022a199b4643be2289dd33cf4e339"
)
POLICY_HASH: str = "8522225273b2696446b698c3caaab6b9a382a3f8a842c3277da18a1199d7ff30"
CEL_SCHEMA_HASH: str = (
    "cb975c6cc14e8df4a1ff729eb2782d5fe118706ecb43d6a069b2871cb1e2e2fb"
)
VECTOR_HASH: str = "dea9bf3a38b291c99f100b9bcf7d9b10864769902363eb643122cc49c5ccfd97"

PUBLISHED_HASHES: dict[str, str] = {
    "VcsRef.digest-vector.json": (
        "37fb01577e0e9b3152b9e1b79832cf73bcbe8ace9a4ac60c04189e0d6f1c0289"
    ),
    "VcsRef.revision-1.json": (
        "c7454de60a8bce7f06f1d0e98b5c5cb796ed45a7a8020d0c0e1f64fdbba83051"
    ),
    "VcsRef.schema.json": (
        "6cb29467d126b9a5f324408568d6925795946dde67ba927244a844f3169a86a8"
    ),
    "VcsRef.url-vectors.json": (
        "2b64d201f50890c7c9b6a06baf6bc47d2846e15bf5661c4725f2be6bcb09eac4"
    ),
    "StructuredOutput.revision-1.json": (
        "198db78bc94c844acab7f61eeb610845f65ea9e93217af566ed5c17246ab0f71"
    ),
    "StructuredOutput.schema.json": (
        "e924b9a44c0825f0b2a43b0291d1f98c4851425a50e291914dd85ba096ac6eae"
    ),
    "StructuredOutput.vectors.json": (
        "8ed41a4c96132ee20a12ad70ddf3ef14e4c482dffa7d9a4ceb33d000018bb3ff"
    ),
    "CommandResult.revision-1.json": (
        "855a0d3a5a3df6dc2c6405b7a306562b1f60809714989cc6b19fd70b5e682a12"
    ),
    "CommandResult.schema.json": (
        "8444eeb4d4688b6d059dd7221689dd4c824084c88ff189ee3fbb848153c1f8d4"
    ),
    "CommandResult.vectors.json": (
        "5153f381d0c6fa5208d0ed7e1bf765249120ea2db964a8f79e0a67925b5a4849"
    ),
}

FIXTURE_HASHES: dict[str, str] = {
    "examples/agent-session/captured-complete.json": (
        "4e5c56df3073bfd6cb4439ff49ee421bff298293ee6758ce17f4117beb5beed0"
    ),
    "examples/agent-session/captured-empty.json": (
        "42fdcdaab09bf877971b1fc4fad3b44866ac717cf182929fe611644dd83f3a59"
    ),
    "examples/agent-session/denial-sample-incomplete.json": (
        "45ac8d5aaeb7fd1b396954bfd1c2d40fc8a3c281396e1c9a8c51a8bf5392b5f6"
    ),
    "examples/agent-session/invalid/changed-retained-size.json": (
        "8231a071a2e65bd9f04bbfc49f569683e04476191f9eef547a3e94cd91a1773a"
    ),
    "examples/agent-session/invalid/claimed-source-verification.json": (
        "47104469a2f78d8ad301475fdca3a4018a2a1747b6aab19a4892eb79c750cc58"
    ),
    "examples/agent-session/invalid/complete-with-gap.json": (
        "3c76d763c93d0f9530229851f08c0edf4c67cfe698401b82a857ae488edcba06"
    ),
    "examples/agent-session/invalid/denial-count-underflow.json": (
        "1bcc86fad3da04e23d5742989fa01c87d9c6d35ac125d85d13602dc3f8bbde18"
    ),
    "examples/agent-session/invalid/denial-text.json": (
        "2af185d551ace200103615ace14722c26b34948fb12eb206f2c95610cf204d09"
    ),
    "examples/agent-session/invalid/float-duration.json": (
        "70bfcc429829a3835a2dc973f48321ee0ead72c0ffc26dc029123ce4ead0a09d"
    ),
    "examples/agent-session/invalid/negative-duration.json": (
        "85efba66e037597bb652143cf5b33482aaa971c85bd11285b60d1b68f059c70c"
    ),
    "examples/agent-session/invalid/subscription-numeric-usd.json": (
        "83cee988118020ad9a4064e2919c39bf3da0d06c4b7ca77190d2f0aa9875b0ba"
    ),
    "examples/agent-session/invalid/unknown-transcript-reference.json": (
        "004f47436af8b15b4e0670dd2daae87f837fff82d8923e7b75196e78d441635f"
    ),
    "examples/agent-session/invalid/usage-source.json": (
        "3557310b4e4f4ab7c48ab7448ba45544eb541bfbd2559c0f9b0e414b2d90d16c"
    ),
    "examples/agent-session/subscription-null-usd.json": (
        "4cb5a15658eafad5346c3cbee9da2e05b9bda57930f06e22dc541e9ffd3d7749"
    ),
    "examples/agent-session/unavailable-no-usage.json": (
        "ef90fdfc01ded2dd9fc0f4a1aad255cc714abddf7dc498f8310687b58d226795"
    ),
}


class SchemaValidator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


class SchemaClass(Protocol):
    def check_schema(self, schema: object) -> None: ...


def test_agent_session_contract_files_are_consistent() -> None:
    schema_bytes = (ARTIFACTS / "AgentSession.schema.json").read_bytes()
    descriptor_bytes = (ARTIFACTS / "AgentSession.revision-1.json").read_bytes()
    schema = cast("dict[str, object]", json.loads(schema_bytes))
    descriptor: object = json.loads(descriptor_bytes)
    expected_descriptor: dict[str, object] = {
        "name": "AgentSession",
        "builtin_revision": "1",
        "schema": "urn:kora:artifact:AgentSession",
        "parent": None,
        "representation": "kora.agent-session-transcript-primary.v1",
        "behavior": {
            "validation": "kora.agent-session-validation.v1",
            "digest_fields": "kora.agent-session-logical.v1",
            "summary": "kora.agent-session-summary.v1",
        },
        "canonicalization": "kora.structured-json.v1",
        "validation": {
            "max_raw_logical_json_bytes": 4_194_304,
            "max_canonical_logical_json_bytes": 4_194_304,
            "max_json_depth": 16,
        },
        "digest_selection": [""],
        "logical_identity": "kora.agent-session-logical.v1",
        "compatibility": {
            "execution_semantics": "kora.execution.v1",
            "checker_view": "kora.agent-session-view.v1",
        },
        "schema_sha256": SCHEMA_HASH,
        "cel_policy_sha256": POLICY_HASH,
    }
    vectors = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "AgentSession.vectors.json").read_bytes()),
    )
    invalid_cases = cast("list[dict[str, str]]", vectors["invalid_cases"])
    valid_paths = sorted((ARTIFACTS / "examples/agent-session").glob("*.json"))
    invalid_paths = sorted(
        (ARTIFACTS / "examples/agent-session/invalid").glob("*.json")
    )
    valid_values: list[object] = [json.loads(path.read_bytes()) for path in valid_paths]
    invalid_values: list[object] = [
        json.loads((ARTIFACTS / case["file"]).read_bytes()) for case in invalid_cases
    ]
    schema_definitions = cast("dict[str, object]", schema["$defs"])
    references = re.findall(r'"\$ref": "([^"]+)"', schema_bytes.decode("utf-8"))
    example_documents = [
        cast("dict[str, object]", load_yaml(path))
        for path in sorted(EXAMPLES.glob("*.ir.yaml"))
    ]
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )

    cast("SchemaClass", Draft202012Validator).check_schema(schema)
    accepted_valid = [validator.is_valid(value) for value in valid_values]
    accepted_invalid = [validator.is_valid(value) for value in invalid_values]
    example_bindings = [
        cast("dict[str, object]", document["type_contracts"])["AgentSession"]
        for document in example_documents
    ]

    assert sha256(schema_bytes).hexdigest() == SCHEMA_HASH
    assert sha256(descriptor_bytes).hexdigest() == DESCRIPTOR_HASH
    assert descriptor == expected_descriptor
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == expected_descriptor["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(references) <= {f"#/$defs/{name}" for name in schema_definitions}
    assert len(valid_paths) == 5
    assert len(invalid_paths) == 10
    assert {case["file"] for case in invalid_cases} == {
        path.relative_to(ARTIFACTS).as_posix() for path in invalid_paths
    }
    assert all(accepted_valid)
    assert accepted_invalid == [
        case["refusal_layer"] == "semantic" for case in invalid_cases
    ]
    assert len(example_documents) == 2
    assert example_bindings == [{"builtin_revision": "1"}] * 2
    for name, digest in PUBLISHED_HASHES.items():
        assert sha256((ARTIFACTS / name).read_bytes()).hexdigest() == digest


def test_agent_session_contract_vectors_are_immutable() -> None:
    vector_bytes = (ARTIFACTS / "AgentSession.vectors.json").read_bytes()
    vectors = cast("dict[str, object]", json.loads(vector_bytes))
    canonical_cases = cast("list[dict[str, object]]", vectors["canonical_cases"])
    replay_cases = cast("list[dict[str, object]]", vectors["replay_cases"])
    fixture_paths = sorted((ARTIFACTS / "examples/agent-session").rglob("*.json"))
    canonical_results = [
        (
            cast("str", case["canonical"]).encode("utf-8"),
            json.loads(
                (
                    ARTIFACTS / "examples/agent-session" / f"{case['name']}.json"
                ).read_bytes()
            ),
            case,
        )
        for case in canonical_cases
    ]
    baseline = cast("dict[str, object]", json.loads(canonical_results[0][0]))
    baseline_transcript = cast("dict[str, object]", baseline["transcript"])

    fixture_hashes = {
        path.relative_to(ARTIFACTS).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in fixture_paths
    }
    replay_hashes = [
        sha256(
            json.dumps(
                case["candidate"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for case in replay_cases
        if "candidate" in case
    ]

    assert sha256(vector_bytes).hexdigest() == VECTOR_HASH
    assert vectors["contract"] == "urn:kora:artifact:AgentSession"
    assert vectors["builtin_revision"] == "1"
    assert vectors["canonicalization"] == "kora.structured-json.v1"
    assert fixture_hashes == FIXTURE_HASHES
    assert len({case["name"] for case in canonical_cases}) == len(canonical_cases) == 5
    for wire, fixture, case in canonical_results:
        assert json.loads(wire) == fixture
        assert len(wire) == case["canonical_bytes"]
        assert sha256(wire).hexdigest() == case["logical_sha256"]
        assert case["approval_sha256"] == case["logical_sha256"]
        transcript = cast(
            "dict[str, object]", cast("dict[str, object]", fixture)["transcript"]
        )
        assert transcript.get("retained_sha256") == case["retained_transcript_sha256"]
    changed_cases = [case for case in replay_cases if "candidate" in case]
    assert len(changed_cases) == 4
    assert replay_hashes == [case["logical_sha256"] for case in changed_cases]
    for case in changed_cases:
        candidate = cast("dict[str, object]", case["candidate"])
        transcript = cast("dict[str, object]", candidate["transcript"])
        assert transcript["retained_sha256"] == baseline_transcript["retained_sha256"]
        assert case["retained_transcript_sha256_unchanged"] is True
        assert case["logical_sha256"] != canonical_cases[0]["logical_sha256"]
        assert case["outcome"] == "conflict"
    assert len(replay_cases) == 7
    assert replay_cases[4]["logical_sha256"] == canonical_cases[0]["logical_sha256"]
    assert vectors["truncation_cases"] == [
        {
            "name": "exact-cap",
            "source_bytes": 268_435_456,
            "truncated": False,
            "source_verified": True,
            "rule": "all source bytes received and verified",
        },
        {
            "name": "over-cap",
            "source_bytes": 268_435_457,
            "retained_prefix_bytes": 268_435_432,
            "marker_ascii": "\n[transcript truncated]\n",
            "marker_bytes": 24,
            "retained_bytes": 268_435_456,
            "truncated": True,
            "source_verified": False,
            "rule": (
                "retain declared source identity as unverified; "
                "no complete-source digest assertion; "
                "original completion binding survives expiry"
            ),
        },
    ]


def test_agent_session_cel_policy_is_closed() -> None:
    policy_bytes = (ARTIFACTS / "AgentSession.cel-policy.json").read_bytes()
    schema_bytes = (ARTIFACTS / "AgentSession.cel.schema.json").read_bytes()
    policy = cast("dict[str, object]", json.loads(policy_bytes))
    schema = cast("dict[str, object]", json.loads(schema_bytes))
    definitions = cast("dict[str, dict[str, object]]", schema["$defs"])
    vectors = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "AgentSession.vectors.json").read_bytes()),
    )
    canonical_cases = cast("list[dict[str, object]]", vectors["canonical_cases"])
    whole_fields = [
        "harness",
        "installed_version",
        "session_id",
        "billing_class",
        "turns",
        "duration_ms",
        "stop_reason",
        "provenance",
    ]
    usage_fields = [
        "model",
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_tokens",
        "output_tokens",
        "reasoning_tokens",
        "known_token_fields",
        "cache_creation_by_ttl",
        "completeness",
        "basis",
        "billing_class",
        "delta_of_session_totals",
        "usd_estimate",
    ]
    coverage_fields = ["completeness", "attribution_gap", "observation_gap"]
    expected_policy: dict[str, object] = {
        "identity": "kora.agent-session-view.v1",
        "root": "metadata",
        "whole_fields": whole_fields,
        "usage_fields": usage_fields,
        "usage_coverage_fields": coverage_fields,
        "excluded": [
            "denials",
            "transcript",
            "metadata.price_table_id",
            "metadata.usage[*].reported_usd",
            "metadata.usage[*].observation_id",
            "metadata.usage[*].observation_sequence",
            "metadata.usage[*].observed_at",
            "metadata.usage_coverage.version",
            "metadata.usage_coverage.observation_id",
            "metadata.usage_coverage.observation_sequence",
            "metadata.usage_coverage.observed_at",
        ],
        "cel_schema_sha256": CEL_SCHEMA_HASH,
    }
    references = re.findall(r'"\$ref": "([^"]+)"', schema_bytes.decode("utf-8"))
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )

    cast("SchemaClass", Draft202012Validator).check_schema(schema)
    accepted_views = [
        validator.is_valid(case["permanent_view"]) for case in canonical_cases
    ]
    fixture_views: list[dict[str, object]] = []
    for case in canonical_cases:
        logical = cast("dict[str, object]", json.loads(cast("str", case["canonical"])))
        metadata = cast("dict[str, object]", logical["metadata"])
        coverage = cast("dict[str, object]", metadata["usage_coverage"])
        view = {name: metadata[name] for name in whole_fields}
        view["usage"] = [
            {name: row[name] for name in usage_fields}
            for row in cast("list[dict[str, object]]", metadata["usage"])
        ]
        view["usage_coverage"] = {name: coverage[name] for name in coverage_fields}
        fixture_views.append(view)
    view_hashes = [
        sha256(
            json.dumps(
                case["permanent_view"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for case in canonical_cases
    ]

    assert sha256(policy_bytes).hexdigest() == POLICY_HASH
    assert sha256(schema_bytes).hexdigest() == CEL_SCHEMA_HASH
    assert policy == expected_policy
    assert schema["$id"] == "urn:kora:artifact:AgentSession:cel:1"
    assert schema["additionalProperties"] is False
    assert set(cast("dict[str, object]", schema["properties"])) == set(whole_fields) | {
        "usage",
        "usage_coverage",
    }
    assert set(cast("list[str]", schema["required"])) == set(
        cast("dict[str, object]", schema["properties"])
    )
    assert set(references) <= {f"#/$defs/{name}" for name in definitions}
    for name, fields in (("usage", usage_fields), ("coverage", coverage_fields)):
        assert definitions[name]["additionalProperties"] is False
        assert set(cast("dict[str, object]", definitions[name]["properties"])) == set(
            fields
        )
        assert set(cast("list[str]", definitions[name]["required"])) == set(fields)
    assert all(accepted_views)
    assert fixture_views == [case["permanent_view"] for case in canonical_cases]
    assert view_hashes == [case["permanent_view_sha256"] for case in canonical_cases]
