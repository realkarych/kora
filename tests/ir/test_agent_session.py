import json
import traceback
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from typing import Never, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError
from referencing import Registry

from kora.ir import (
    AgentSession,
    AgentSessionValidationError,
    agent_session_digest,
    canonical_agent_session,
    parse_agent_session,
    parse_agent_session_json,
    require_agent_session_revision,
    summarize_agent_session,
    verify_agent_session_transcript,
)
from kora.ir.agent_session import (
    SUMMARY_COVERAGE_FIELDS,
    SUMMARY_FIELDS,
    SUMMARY_USAGE_FIELDS,
)
from kora.ir.cost import Identifier as CostIdentifier
from kora.ir.json import JsonObject
from kora.ir.structured_json import (
    StructuredOutputValidationError,
    decode_json,
    snapshot,
)
from kora.protocols.identity_records import Identifier
from tests.ir.agent_session_cases import (
    aliased,
    changed,
    dense,
    first_usage,
    fixture,
    fixture_bytes,
    forged,
    metadata,
    rows,
    with_transcript,
)
from tests.ir.test_agent_session_contract import ARTIFACTS, SchemaValidator


@pytest.mark.parametrize(
    "case", rows("canonical_cases"), ids=lambda case: str(case["name"])
)
def test_agent_session_canonical_logical_value(case: dict[str, object]) -> None:
    raw = cast("str", case["canonical"]).encode()
    source: object = json.loads(raw)

    native = AgentSession(source)
    parsed = parse_agent_session_json(raw)

    assert canonical_agent_session(native) == canonical_agent_session(parsed) == raw
    assert (
        agent_session_digest(parsed)
        == case["logical_sha256"]
        == case["approval_sha256"]
    )
    assert summarize_agent_session(parsed) == case["permanent_view"]
    assert len(raw) == case["canonical_bytes"]
    assert parse_agent_session(parsed).value == source


def test_agent_session_schema_matches_model() -> None:
    schema = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "AgentSession.schema.json").read_bytes()),
    )
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )
    descriptor: object = json.loads(
        (ARTIFACTS / "AgentSession.revision-1.json").read_bytes()
    )
    candidates = [
        fixture(cast("str", case["name"])) for case in rows("canonical_cases")
    ]
    candidates.append(dense())

    require_agent_session_revision(descriptor)
    parsed = [parse_agent_session(value) for value in candidates]

    assert all(validator.is_valid(value.value) for value in parsed)
    for case in rows("invalid_cases"):
        if case["refusal_layer"] == "structural":
            value: object = json.loads(
                (ARTIFACTS / cast("str", case["file"])).read_bytes()
            )
            assert not validator.is_valid(value)


@pytest.mark.parametrize(
    "path",
    sorted((ARTIFACTS / "examples/agent-session/invalid").glob("*.json")),
    ids=lambda path: path.stem,
)
def test_agent_session_rejects_invalid_payload(path: Path) -> None:
    raw = path.read_bytes()

    with pytest.raises(AgentSessionValidationError):
        parse_agent_session_json(raw)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("metadata", "duration_ms"), True),
        (("metadata", "duration_ms"), 1.0),
        (("metadata", "duration_ms"), "1"),
        (("metadata", "duration_ms"), 2**63),
        (("metadata", "turns"), 2**31),
        (("metadata", "harness"), "bad\n"),
        (("metadata", "session_id"), "é"),
        (("metadata", "price_table_id"), "a\u200b"),
        (("metadata", "provenance", "image_digest"), "a\x7f"),
        (("metadata", "stop_reason"), "Bad"),
        (("metadata", "usage", 0, "input_tokens"), True),
        (("metadata", "usage", 0, "input_tokens"), -1),
        (("metadata", "usage", 0, "input_tokens"), 2**31),
        (("metadata", "usage", 0, "reasoning_tokens"), 1),
        (("metadata", "usage", 0, "observation_sequence"), 2**53),
        (("metadata", "usage", 0, "observed_at"), "2026-10-29T12:00:00+00:00"),
        (("metadata", "usage", 0, "observed_at"), "2026-10-29T12:00:00.0Z"),
        (("metadata", "usage", 0, "observed_at"), "2026-02-30T12:00:00Z"),
        (
            ("metadata", "usage", 0, "known_token_fields"),
            ["input_tokens", "input_tokens"],
        ),
        (
            ("metadata", "usage", 0, "known_token_fields"),
            ["output_tokens", "input_tokens"],
        ),
        (("metadata", "usage", 0, "cache_creation_by_ttl"), {"5m": 1, "1h": 0}),
        (("metadata", "usage", 0, "cache_creation_by_ttl"), {"5m": False, "1h": 0}),
        (("metadata", "usage", 0, "usd_estimate"), "00"),
        (("metadata", "usage", 0, "usd_estimate"), "0.10"),
        (("metadata", "usage", 0, "usd_estimate"), 0),
        (("metadata", "usage_coverage", "version"), True),
        (("metadata", "usage_coverage", "observation_sequence"), None),
        (("metadata", "usage_coverage", "attribution_gap"), True),
        (("transcript", "source_verified"), False),
        (("transcript", "retained_sha256"), "0" * 64),
        (("transcript", "retained_size"), 268435457),
        (("transcript", "source_sha256"), "a" * 64 + "\n"),
        (("denials", "complete"), 1),
        (("denials", "items"), ()),
    ],
)
def test_agent_session_semantic_refusals(
    path: tuple[str | int, ...], replacement: object
) -> None:
    value = changed(path, replacement)

    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(value)


@pytest.mark.parametrize(
    "basis", ["harness_list", "harness_managed", "price_table", "gateway"]
)
def test_agent_session_estimate_matches_available_evidence(basis: str) -> None:
    value = fixture()
    row = first_usage(value)
    row["basis"] = basis
    if basis in {"harness_list", "harness_managed"}:
        row["reported_usd"] = "1"
    else:
        row["known_token_fields"] = []
        row["cache_creation_by_ttl"] = None

    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(value)

    row["usd_estimate"] = row["reported_usd"] if basis.startswith("harness_") else None
    assert parse_agent_session(value).value == value


def test_agent_session_nullable_coverage_time_and_equal_rate_evidence() -> None:
    source = fixture()
    cast("JsonObject", metadata(source)["usage_coverage"])["observed_at"] = None
    row = first_usage(source)
    row["cache_creation_by_ttl"] = None
    row["known_token_fields"] = [
        "cache_creation_tokens",
        "cached_input_tokens",
        "input_tokens",
        "output_tokens",
    ]

    value = parse_agent_session(source)

    assert value.value == source


@pytest.mark.parametrize(
    "text",
    [
        "a",
        "a" * 256,
        "a" * 257,
        "a\n",
        "a b",
        "é",
        "🦉",
        "\u200b",
        "\x7f",
        "",
        "\ud800",
    ],
)
@pytest.mark.parametrize("cost", [True, False])
def test_agent_session_identifier_predicate_parity(text: str, cost: bool) -> None:
    source = fixture()
    path = "price_table_id" if cost else "harness"
    metadata(source)[path] = text
    adapter = TypeAdapter(CostIdentifier if cost else Identifier)
    accepted = True
    try:
        adapter.validate_python(text)
    except ValidationError:
        accepted = False

    if accepted:
        parsed = parse_agent_session(source)
        assert metadata(parsed.value)[path] == text
    else:
        with pytest.raises(AgentSessionValidationError):
            parse_agent_session(source)


@pytest.mark.parametrize(
    ("source", "code", "pattern", "accepted"),
    [
        ("harness", "harness_rule", None, True),
        ("harness", "policy_timeout", None, True),
        ("harness", "denied", None, False),
        ("policy", "harness_rule", None, False),
        ("policy", "allowed", None, False),
        ("policy", "protected_path", None, True),
        ("policy", "denied", "node.deny.9999", True),
        ("policy", "denied", "node.allow.1", False),
        ("policy", "denied", "node.deny.01", False),
        ("policy", "denied", "node.deny.10000", False),
        ("policy", "protected_path", "node.deny.0", False),
        ("harness", "harness_rule", "node.deny.0", False),
    ],
)
def test_agent_session_denial_predicates(
    source: str, code: str, pattern: str | None, accepted: bool
) -> None:
    value = fixture()
    value["denials"] = {
        "items": [
            {
                "tool": "Bash",
                "tool_use_id": "call",
                "source": source,
                "code": code,
                "pattern": pattern,
            }
        ],
        "total": 2,
        "complete": True,
    }

    if accepted:
        assert parse_agent_session(value).value == value
    else:
        with pytest.raises(AgentSessionValidationError):
            parse_agent_session(value)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf{}",
        b'"\xff"',
        b'"\\ud800"',
        b"[" * 17 + b"]" * 17,
        b"NaN",
        b"1e9999",
        fixture_bytes()[:-2] + b',"metadata":{}}',
    ],
)
def test_agent_session_raw_encoding_refusals(raw: bytes) -> None:
    with pytest.raises(AgentSessionValidationError):
        parse_agent_session_json(raw)


def test_agent_session_distinct_codec_budgets() -> None:
    value = dense()
    wire = canonical_agent_session(parse_agent_session(value))
    padded = wire + b" " * (4194304 - len(wire))

    parsed = parse_agent_session_json(padded)

    assert 1048576 < len(wire) < 4194304
    assert canonical_agent_session(parsed) == wire
    with pytest.raises(StructuredOutputValidationError):
        decode_json(wire)
    with pytest.raises(StructuredOutputValidationError):
        snapshot(value)
    with pytest.raises(AgentSessionValidationError) as failure:
        parse_agent_session_json(padded + b" ")
    assert failure.value.violation.code == "AS001"
    with pytest.raises(AgentSessionValidationError):
        parse_agent_session_json(padded[:-1] + b"!")


def test_agent_session_usage_order_and_uniqueness() -> None:
    value = fixture()
    row = first_usage(value)
    metadata(value)["usage"] = [row, row]

    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(value)

    row["model"] = "z"
    metadata(value)["usage"] = [row, {**row, "model": "a"}]
    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(value)


def test_agent_session_detached_ownership() -> None:
    source = aliased()
    value = parse_agent_session(source)
    before = canonical_agent_session(value)
    detached = value.value
    summary = summarize_agent_session(value)

    metadata(source)["harness"] = "changed"
    metadata(detached)["harness"] = "changed"
    cast("JsonObject", summary["provenance"])["worker_id"] = "changed"
    cast("list[JsonObject]", summary["usage"])[0]["known_token_fields"] = []

    assert canonical_agent_session(value) == before
    assert repr(value) == "AgentSession()"
    with pytest.raises(FrozenInstanceError):
        assign_field(value, "_canonical", b"{}")


def assign_field(value: object, name: str, item: object) -> None:
    setattr(value, name, item)


def test_agent_session_summary_matches_frozen_policy() -> None:
    policy = cast(
        "dict[str, list[str]]",
        json.loads((ARTIFACTS / "AgentSession.cel-policy.json").read_bytes()),
    )
    value = parse_agent_session(dense())

    summary = summarize_agent_session(value)

    assert list(SUMMARY_FIELDS) == policy["whole_fields"]
    assert list(SUMMARY_USAGE_FIELDS) == policy["usage_fields"]
    assert list(SUMMARY_COVERAGE_FIELDS) == policy["usage_coverage_fields"]
    assert set(summary) == {*SUMMARY_FIELDS, "usage", "usage_coverage"}
    assert set(cast("list[JsonObject]", summary["usage"])[0]) == set(
        SUMMARY_USAGE_FIELDS
    )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("metadata", "duration_ms"), 1501),
        (("metadata", "price_table_id"), "other"),
        (("metadata", "usage", 0, "observation_id"), "later"),
        (("denials", "complete"), False),
        (("denials", "total"), 1),
        (("transcript",), {"capture": "unavailable"}),
    ],
)
def test_agent_session_digest_binds_all_sections(
    path: tuple[str | int, ...], replacement: object
) -> None:
    original = parse_agent_session(fixture())
    revised = changed(path, replacement)

    result = parse_agent_session(revised)

    assert agent_session_digest(result) != agent_session_digest(original)


def detached(value: AgentSession) -> JsonObject:
    return value.value


def verify_empty(value: AgentSession) -> None:
    verify_agent_session_transcript(value, b"")


@pytest.mark.parametrize(
    "action",
    [
        canonical_agent_session,
        agent_session_digest,
        summarize_agent_session,
        parse_agent_session,
        detached,
        verify_empty,
    ],
)
@pytest.mark.parametrize(
    "raw",
    [None, b"{}", fixture_bytes(), b" " * 4194305],
    ids=("missing", "empty-object", "noncanonical", "over-raw-limit"),
)
def test_agent_session_forged_exports(
    action: Callable[[AgentSession], object], raw: object
) -> None:
    value = forged(raw)

    with pytest.raises(AgentSessionValidationError):
        action(value)


def test_agent_session_missing_state() -> None:
    value = object.__new__(AgentSession)

    with pytest.raises(AgentSessionValidationError):
        value.canonical_bytes()


@pytest.mark.parametrize("change", ["extra", "integer", "schema", "view", "cycle"])
def test_agent_session_descriptor_tampering(change: str) -> None:
    descriptor = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "AgentSession.revision-1.json").read_bytes()),
    )
    if change == "integer":
        cast("dict[str, object]", descriptor["validation"])["max_json_depth"] = 16.0
    elif change == "schema":
        descriptor["schema_sha256"] = "0" * 64
    elif change == "view":
        cast("dict[str, object]", descriptor["compatibility"])["checker_view"] = "new"
    elif change == "cycle":
        descriptor["behavior"] = descriptor
    else:
        descriptor["extra"] = True

    with pytest.raises(AgentSessionValidationError) as failure:
        require_agent_session_revision(descriptor)

    assert failure.value.violation.code == "AS005"


@pytest.mark.parametrize("kind", ["raw", "timestamp", "money", "identifier"])
def test_agent_session_safe_diagnostics(kind: str) -> None:
    source = fixture()
    if kind == "timestamp":
        first_usage(source)["observed_at"] = "secret-content"
    elif kind == "money":
        first_usage(source)["usd_estimate"] = "secret-content"
    else:
        metadata(source)["price_table_id"] = "secret-content\n"
    raw = b'{"secret-content":' if kind == "raw" else json.dumps(source).encode()
    failure: AgentSessionValidationError | None = None

    try:
        parse_agent_session_json(raw)
    except AgentSessionValidationError as error:
        failure = error

    assert failure is not None
    assert failure.__context__ is None
    assert failure.__cause__ is None
    assert "secret-content" not in str(failure)
    assert "secret-content" not in "".join(traceback.format_exception(failure))
    assert failure.violation.path == ()


class ExternalString(str):
    pass


class ExternalDict(dict[str, object]):
    def __len__(self) -> int:
        raise AssertionError("must not inspect subclass")


class ExternalList(list[object]):
    def __iter__(self) -> Never:
        raise AssertionError("must not iterate subclass")


@pytest.mark.parametrize(
    "candidate",
    [
        ExternalDict(),
        changed(("metadata", "harness"), ExternalString("claude")),
        changed(("denials", "items"), ExternalList()),
        changed(("metadata", "usage"), [None] * 10000),
        changed(("denials", "items"), [None] * 10000),
        changed(("metadata", "harness"), "a" * 4194305),
    ],
)
def test_agent_session_refuses_before_copying(candidate: object) -> None:
    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(candidate)


def test_agent_session_cycle_and_unknown_member() -> None:
    source = cast("dict[str, object]", fixture())
    source["metadata"] = source

    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(source)

    source = cast("dict[str, object]", fixture())
    source["extra"] = source
    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(source)


@pytest.mark.parametrize("size", [0, 1, 24, 4097, 268435456])
def test_agent_session_retained_transcript_integrity(size: int) -> None:
    payload = b"a" * size
    value = parse_agent_session(with_transcript(payload))

    verify_agent_session_transcript(value, payload)

    assert (
        cast("JsonObject", value.value["transcript"])["source_sha256"]
        == sha256(payload).hexdigest()
    )


def test_agent_session_transcript_full_digest_detects_prefix_corruption() -> None:
    payload = b"a" * 4097
    value = parse_agent_session(with_transcript(payload))

    with pytest.raises(AgentSessionValidationError):
        verify_agent_session_transcript(value, b"b" + payload[1:])


def test_agent_session_truncated_retained_marker_and_source_identity() -> None:
    marker = b"\n[transcript truncated]\n"
    payload = b"a" * (268435456 - len(marker)) + marker
    source = with_transcript(payload, truncated=True)
    value = parse_agent_session(source)

    verify_agent_session_transcript(value, payload)
    cast("JsonObject", source["transcript"])["source_sha256"] = "1" * 64
    other = parse_agent_session(source)

    assert agent_session_digest(value) != agent_session_digest(other)
    assert cast("JsonObject", other.value["transcript"])["source_verified"] is False


def test_agent_session_truncated_marker_is_checked_even_with_matching_digest() -> None:
    payload = b"a" * 268435456
    value = parse_agent_session(with_transcript(payload, truncated=True))

    with pytest.raises(AgentSessionValidationError):
        verify_agent_session_transcript(value, payload)


def test_agent_session_absence_and_transcript_preflight() -> None:
    absent = parse_agent_session(fixture("unavailable-no-usage"))
    invalid = forged(None)
    oversized = b"a" * 268435457

    with pytest.raises(AgentSessionValidationError):
        verify_agent_session_transcript(absent, b"")
    with pytest.raises(AgentSessionValidationError) as failure:
        verify_agent_session_transcript(invalid, oversized)

    assert failure.value.violation.code == "AS006"


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens"],
)
def test_agent_session_unknown_required_dimension_cannot_have_an_estimate(
    field: str,
) -> None:
    source = fixture()
    row = first_usage(source)
    known = cast("list[str]", row["known_token_fields"])
    known.remove(field)
    row["cache_creation_by_ttl"] = None

    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(source)

    row["usd_estimate"] = None
    assert parse_agent_session(source).value == source


@pytest.mark.parametrize(
    ("path", "count"), [(("metadata", "usage"), 65), (("denials", "items"), 1001)]
)
def test_agent_session_collection_one_over_limit(
    path: tuple[str | int, ...], count: int
) -> None:
    source = changed(path, [None] * count)

    with pytest.raises(AgentSessionValidationError):
        parse_agent_session(source)


def test_agent_session_denial_sample_order_is_logical_identity() -> None:
    source = fixture("denial-sample-incomplete")
    denials = cast("JsonObject", source["denials"])
    first = cast("list[JsonObject]", denials["items"])[0]
    denials["items"] = [first, {**first, "tool_use_id": "other-call"}]
    original = parse_agent_session(source)

    denials["items"] = list(reversed(cast("list[JsonObject]", denials["items"])))
    revised = parse_agent_session(source)

    assert agent_session_digest(original) != agent_session_digest(revised)
    assert summarize_agent_session(original) == summarize_agent_session(revised)


class ExternalBytes(bytes):
    pass


def test_agent_session_bytes_subclass_refuses_without_reading_model() -> None:
    payload = ExternalBytes(b"")
    value = forged(None)

    with pytest.raises(AgentSessionValidationError) as failure:
        verify_agent_session_transcript(value, payload)

    assert failure.value.violation.code == "AS006"
    with pytest.raises(AgentSessionValidationError) as raw_failure:
        parse_agent_session_json(payload)
    assert raw_failure.value.violation.code == "AS002"
