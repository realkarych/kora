import dataclasses
import json
import traceback
from collections.abc import Callable
from typing import cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import (
    AgentSessionValidationError,
    agent_session_digest,
    parse_agent_session,
    parse_agent_session_json,
    summarize_agent_session,
    verify_agent_session_transcript,
)
from kora.ir.data import detach_ir_data
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.artifact_type.agent_session import AgentSessionArtifactType
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.ir.agent_session_cases import (
    changed,
    dense,
    first_usage,
    fixture,
    fixture_bytes,
    metadata,
    rows,
    with_transcript,
)
from tests.ir.test_agent_session_contract import ARTIFACTS
from tests.plugins.artifact_type.agent_session_support import (
    INVALID_MARKER,
    MARKERS,
    binding,
    expected_summary,
    lifetime_shapes,
    payload,
    transcript_descriptor,
)
from tests.plugins.artifact_type.command_result_support import (
    binding as command_binding,
)
from tests.plugins.artifact_type.support import binding as vcs_ref_binding
from tests.support.observability import run_python


def provider() -> AgentSessionArtifactType:
    return AgentSessionArtifactType({}, binding())


def nested(document: JsonObject, name: str) -> JsonObject:
    return cast("JsonObject", document[name])


def changed_descriptor(
    path: tuple[str, ...], value: JsonValue
) -> BuiltinArtifactTypeBinding:
    supplied = binding()
    target = supplied.revision_descriptor
    for name in path[:-1]:
        target = nested(target, name)
    target[path[-1]] = value
    return supplied


def descriptor_paths(
    value: JsonObject, prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    result: list[tuple[str, ...]] = []
    for name, item in value.items():
        path = (*prefix, name)
        if isinstance(item, dict):
            result.extend(descriptor_paths(item, path))
        else:
            result.append(path)
    return result


def reverse_members(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {name: reverse_members(item) for name, item in reversed(value.items())}
    if isinstance(value, list):
        return [reverse_members(item) for item in value]
    return value


class ExternalBinding(BuiltinArtifactTypeBinding):
    pass


class ExternalString(str):
    pass


class ExternalDict(dict[str, JsonValue]):
    pass


class ExternalList(list[JsonValue]):
    pass


@pytest.mark.parametrize("path", descriptor_paths(binding().revision_descriptor))
def test_agent_session_provider_refuses_incompatible_binding(
    path: tuple[str, ...],
) -> None:
    supplied = changed_descriptor(path, "private-binding-marker")

    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert raised.value.message == "the artifact type binding is incompatible"
    assert raised.value.details == {"rule": "revision_descriptor"}
    assert "private-binding-marker" not in str(raised.value)
    assert "private-binding-marker" not in json.dumps(raised.value.details)
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("name", "other"),
        ("name", ExternalString("AgentSession")),
        ("parent", "other"),
        ("parent", False),
        ("schema", {"builtin_revision": "1"}),
        ("schema", ExternalDict(binding().schema)),
        ("revision_descriptor", {"builtin_revision": "1"}),
        ("revision_descriptor", ExternalDict(binding().revision_descriptor)),
    ],
)
def test_agent_session_provider_requires_expanded_binding(
    field: str, replacement: object
) -> None:
    original = binding()
    supplied = dataclasses.replace(original, **{field: replacement})

    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize(
    "supplied",
    [
        None,
        {},
        "private-binding-marker",
        True,
        ExternalBinding(**vars(binding())),
    ],
)
def test_agent_session_provider_requires_binding_type(supplied: object) -> None:
    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, cast("BuiltinArtifactTypeBinding", supplied))

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert raised.value.details == {"rule": "binding"}
    assert raised.value.__context__ is raised.value.__cause__ is None


def test_agent_session_provider_rejects_cross_type_binding() -> None:
    with pytest.raises(KoraProtocolError) as vcs_binding_refusal:
        AgentSessionArtifactType({}, vcs_ref_binding())
    with pytest.raises(KoraProtocolError) as command_binding_refusal:
        AgentSessionArtifactType({}, command_binding())
    with pytest.raises(KoraProtocolError) as vcs_ref_refusal:
        VcsRefArtifactType({}, binding())
    with pytest.raises(KoraProtocolError) as command_refusal:
        CommandResultArtifactType({}, binding())

    assert vcs_binding_refusal.value.code == "artifact_type_binding_incompatible"
    assert command_binding_refusal.value.code == "artifact_type_binding_incompatible"
    assert command_refusal.value.code == "artifact_type_binding_incompatible"
    assert vcs_ref_refusal.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("field", ["schema", "revision_descriptor"])
@pytest.mark.parametrize("shape", ["wide", "deep", "cyclic"])
def test_agent_session_provider_refuses_wide_and_deep_bindings_before_copy(
    field: str, shape: str
) -> None:
    supplied = binding()
    if shape == "wide":
        value: JsonObject = {f"unknown{index}": index for index in range(20_000)}
    else:
        value = {}
        if shape == "cyclic":
            value["unexpected"] = value
        else:
            for _ in range(2000):
                value = {"unexpected": value}
    if shape == "wide":
        invalid = dataclasses.replace(supplied, **{field: value})
    else:
        target = supplied.schema if field == "schema" else supplied.revision_descriptor
        target["properties" if field == "schema" else "behavior"] = value
        invalid = supplied

    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, invalid)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("$defs", "identifier", "minLength"), True),
        (("$defs", "identifier", "minLength"), 1.0),
        (("additionalProperties",), 0),
        (("required",), ExternalList(["metadata", "denials", "transcript"])),
        (("properties",), ExternalDict(nested(binding().schema, "properties"))),
        (("type",), ExternalString("object")),
    ],
)
def test_agent_session_provider_preserves_exact_binding_types(
    path: tuple[str, ...],
    replacement: JsonValue,
) -> None:
    supplied = binding()
    target = supplied.schema
    for name in path[:-1]:
        target = nested(target, name)
    target[path[-1]] = replacement

    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("replacement", [True, 16.0])
def test_agent_session_provider_preserves_descriptor_integer_types(
    replacement: JsonValue,
) -> None:
    supplied = changed_descriptor(("validation", "max_json_depth"), replacement)

    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("keyword", ["title", "x-private-annotation"])
def test_agent_session_provider_compares_schema_annotations(keyword: str) -> None:
    supplied = binding()
    supplied.schema[keyword] = "private-schema-marker"

    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert (
        supplied.revision_descriptor["schema_sha256"]
        == binding().revision_descriptor["schema_sha256"]
    )


def test_agent_session_provider_owns_bound_contract(kora_capture: Capture) -> None:
    supplied = binding()
    configuration: JsonObject = {}
    subject = AgentSessionArtifactType(configuration, supplied)
    expected = provider().schema

    nested(nested(supplied.schema, "$defs"), "identifier")["minLength"] = 0
    nested(supplied.revision_descriptor, "behavior")["summary"] = "changed"
    view = subject.schema
    view.clear()
    other = subject.schema
    nested(nested(other, "$defs"), "identifier")["minLength"] = 0
    configuration["unknown"] = "changed-input"

    assert subject.schema == expected
    assert provider().schema == expected
    assert vars(subject)["_schema"] is not supplied.schema
    assert vars(subject)["_revision_descriptor"] is not supplied.revision_descriptor
    assert vars(subject)["_revision_descriptor"] == binding().revision_descriptor
    assert subject.config_schema == {"type": "object", "additionalProperties": False}
    assert subject.validate(payload(), context_for(kora_capture)) == []


def test_agent_session_provider_accepts_schema_member_order(
    kora_capture: Capture,
) -> None:
    supplied = binding()
    reordered = dataclasses.replace(
        supplied,
        schema=cast("JsonObject", reverse_members(supplied.schema)),
        revision_descriptor=cast(
            "JsonObject", reverse_members(supplied.revision_descriptor)
        ),
    )

    subject = AgentSessionArtifactType({}, reordered)

    assert subject.schema == supplied.schema
    assert subject.validate(payload(), context_for(kora_capture)) == []


@pytest.mark.parametrize(
    "configuration",
    [{"unknown": "private-config-marker"}, {"x": None}, [], None, False],
)
def test_agent_session_provider_configuration_is_closed(configuration: object) -> None:
    supplied = binding()

    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType(cast("JsonObject", configuration), supplied)

    assert raised.value.code == "artifact_type_configuration_invalid"
    assert raised.value.message == "the artifact type configuration is invalid"
    assert raised.value.details == {"rule": "closed_empty_object"}
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert "private-config-marker" not in str(raised.value)
    assert "private-config-marker" not in json.dumps(raised.value.details)


def candidates() -> list[JsonValue]:
    values: list[JsonValue] = [payload(), None, [], 1, True, INVALID_MARKER]
    values.extend(
        fixture(cast("str", case["name"])) for case in rows("canonical_cases")
    )
    values.extend(
        cast(
            "JsonValue",
            json.loads((ARTIFACTS / cast("str", case["file"])).read_bytes()),
        )
        for case in rows("invalid_cases")
    )
    values.extend(
        cast("JsonValue", case["candidate"])
        for case in rows("replay_cases")
        if "candidate" in case
    )
    integer_paths: tuple[tuple[str | int, ...], ...] = (
        ("metadata", "duration_ms"),
        ("metadata", "turns"),
        ("metadata", "usage", 0, "input_tokens"),
        ("metadata", "usage", 0, "cached_input_tokens"),
        ("metadata", "usage", 0, "cache_creation_tokens"),
        ("metadata", "usage", 0, "output_tokens"),
        ("metadata", "usage", 0, "reasoning_tokens"),
        ("metadata", "usage", 0, "observation_sequence"),
        ("metadata", "usage_coverage", "version"),
        ("metadata", "usage_coverage", "observation_sequence"),
        ("denials", "total"),
        ("transcript", "source_size"),
        ("transcript", "retained_size"),
    )
    for path in integer_paths:
        for invalid in (True, 1.0, "1", -1, 2**63):
            values.append(cast("JsonValue", changed(path, invalid)))
    boolean_paths: tuple[tuple[str | int, ...], ...] = (
        ("metadata", "usage", 0, "delta_of_session_totals"),
        ("metadata", "usage_coverage", "attribution_gap"),
        ("metadata", "usage_coverage", "observation_gap"),
        ("denials", "complete"),
        ("transcript", "source_verified"),
        ("transcript", "truncated"),
    )
    for path in boolean_paths:
        for invalid in (0, 1, "false", None):
            values.append(cast("JsonValue", changed(path, invalid)))
    for path, invalid in (
        (("metadata", "usage"), [None] * 65),
        (("denials", "items"), [None] * 1001),
        (("metadata", "turns"), 2**31),
        (("metadata", "usage", 0, "input_tokens"), 2**31),
        (("metadata", "usage", 0, "observation_sequence"), 2**53),
    ):
        values.append(cast("JsonValue", changed(path, invalid)))
    complete = dense()
    complete["transcript"] = transcript_descriptor()
    truncated = fixture()
    truncated["transcript"] = transcript_descriptor(truncated=True)
    values.extend([dense(), complete, truncated])
    return values


@pytest.mark.parametrize("candidate", candidates())
def test_agent_session_provider_matches_model_validation(
    kora_capture: Capture, candidate: JsonValue
) -> None:
    subject = provider()
    try:
        parse_agent_session(candidate)
    except AgentSessionValidationError as error:
        expected = [error.violation]
    else:
        expected = []

    result = subject.validate(candidate, context_for(kora_capture))

    assert result == expected
    assert len(result) <= 1


def test_agent_session_provider_digest_selection_is_stable(
    kora_capture: Capture,
) -> None:
    subject = provider()
    value = payload()
    reordered = dict(reversed(list(value.items())))
    context = context_for(kora_capture)

    first = subject.digest_fields(value, context)
    first.append("/substituted")
    second = subject.digest_fields(reordered, context)

    assert second == [""]
    assert first is not second
    assert agent_session_digest(parse_agent_session(value)) == agent_session_digest(
        parse_agent_session(reordered)
    )


@pytest.mark.parametrize("case", rows("canonical_cases"))
def test_agent_session_provider_summary_matches_frozen_view(
    kora_capture: Capture, case: dict[str, object]
) -> None:
    subject = provider()
    value = fixture(cast("str", case["name"]))
    expected = case["permanent_view"]

    projected = subject.summary(value, context_for(kora_capture))

    assert projected == expected
    assert projected == summarize_agent_session(parse_agent_session(value))
    assert projected != value


def test_agent_session_provider_summary_is_detached(kora_capture: Capture) -> None:
    subject = provider()
    value = payload()
    original = cast("JsonObject", detach_ir_data(value))
    context = context_for(kora_capture)

    first = cast("JsonObject", subject.summary(value, context))
    nested(first, "provenance")["worker_id"] = "changed-output"
    row = cast("list[JsonObject]", first["usage"])[0]
    cast("list[JsonValue]", row["known_token_fields"]).clear()
    nested(row, "cache_creation_by_ttl")["5m"] = 99
    nested(first, "usage_coverage")["attribution_gap"] = True
    assert value == original
    nested(metadata(value), "provenance")["worker_id"] = "changed-input"
    first_usage(value)["model"] = "changed-model"
    nested(metadata(value), "usage_coverage")["observation_id"] = "changed-observation"

    assert subject.summary(payload(), context) == expected_summary()
    assert first != expected_summary()
    assert nested(first, "provenance")["worker_id"] == "changed-output"
    assert row["model"] == first_usage(original)["model"]


def test_agent_session_provider_summary_excludes_retained_and_observation_fields(
    kora_capture: Capture,
) -> None:
    subject = provider()
    policy = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "AgentSession.cel-policy.json").read_bytes()),
    )
    value = payload()

    summary = cast("JsonObject", subject.summary(value, context_for(kora_capture)))

    assert policy["excluded"] == [
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
    ]
    assert set(summary) == set(cast("list[str]", policy["whole_fields"])) | {
        "usage",
        "usage_coverage",
    }
    assert not {"metadata", "denials", "transcript", "price_table_id"} & set(summary)
    for row in cast("list[JsonObject]", summary["usage"]):
        assert set(row) == set(cast("list[str]", policy["usage_fields"]))
        assert not {
            "reported_usd",
            "observation_id",
            "observation_sequence",
            "observed_at",
        } & set(row)
    coverage = nested(summary, "usage_coverage")
    assert set(coverage) == set(cast("list[str]", policy["usage_coverage_fields"]))
    assert not {
        "version",
        "observation_id",
        "observation_sequence",
        "observed_at",
    } & set(coverage)
    assert summary == expected_summary()
    assert all(marker not in json.dumps(summary) for marker in MARKERS)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("metadata", "price_table_id"), "other-price"),
        (("metadata", "usage", 0, "reported_usd"), "1"),
        (("metadata", "usage", 0, "observation_id"), "other-observation"),
        (("metadata", "usage", 0, "observation_sequence"), 3),
        (("metadata", "usage", 0, "observed_at"), "2026-10-30T12:00:00Z"),
        (("metadata", "usage_coverage", "observation_id"), "other-coverage"),
        (("metadata", "usage_coverage", "observation_sequence"), 3),
        (("metadata", "usage_coverage", "observed_at"), "2026-10-30T12:00:00Z"),
        (("denials", "complete"), False),
        (("denials", "total"), 1),
        (("transcript",), {"capture": "unavailable"}),
    ],
)
def test_agent_session_provider_digest_binds_excluded_summary_fields(
    kora_capture: Capture, path: tuple[str | int, ...], replacement: object
) -> None:
    subject = provider()
    original = fixture()
    revised = cast("JsonObject", changed(path, replacement))
    context = context_for(kora_capture)

    selections = [
        subject.digest_fields(value, context) for value in (original, revised)
    ]
    summaries = [subject.summary(value, context) for value in (original, revised)]
    digests = [
        agent_session_digest(parse_agent_session(value))
        for value in (original, revised)
    ]

    assert selections == [[""], [""]]
    assert summaries[0] == summaries[1]
    assert digests[0] != digests[1]


@pytest.mark.parametrize("hook", ["summary", "digest_fields"])
def test_agent_session_provider_extraction_requires_valid_payload(
    kora_capture: Capture, hook: str
) -> None:
    subject = provider()
    action = cast(
        "Callable[[JsonValue, ProtocolContext], object]", getattr(subject, hook)
    )

    with pytest.raises(KoraProtocolError) as raised:
        action({"private": INVALID_MARKER}, context_for(kora_capture))

    assert raised.value.code == "artifact_type_payload_invalid"
    assert raised.value.message == "the artifact type payload is invalid"
    assert raised.value.details == {"violation": "AS003"}
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert INVALID_MARKER not in "".join(traceback.format_exception(raised.value))


def test_agent_session_provider_preserves_admitted_raw_value(
    kora_capture: Capture,
) -> None:
    raw = fixture_bytes()
    admitted = parse_agent_session_json(raw).value
    before = cast("JsonObject", detach_ir_data(admitted))
    subject = provider()
    context = context_for(kora_capture)

    violations = subject.validate(admitted, context)
    projected = subject.summary(admitted, context)
    selected = subject.digest_fields(admitted, context)

    assert violations == []
    assert selected == [""]
    assert admitted == before
    assert set(admitted) == {"metadata", "denials", "transcript"}
    assert projected == summarize_agent_session(parse_agent_session_json(raw))
    assert projected != admitted
    with pytest.raises(AgentSessionValidationError):
        parse_agent_session_json(raw[:-2] + b',"metadata":{}}')


def test_agent_session_provider_keeps_transcript_integrity_with_byte_owner(
    kora_capture: Capture,
) -> None:
    retained = b"a"
    candidate = with_transcript(retained)
    nested(candidate, "transcript")["source_sha256"] = "0" * 64
    nested(candidate, "transcript")["retained_sha256"] = "0" * 64
    subject = provider()
    value = parse_agent_session(candidate)

    violations = subject.validate(candidate, context_for(kora_capture))
    with pytest.raises(AgentSessionValidationError) as raised:
        verify_agent_session_transcript(value, retained)

    assert violations == []
    assert (
        nested(candidate, "transcript")["source_sha256"]
        == nested(candidate, "transcript")["retained_sha256"]
    )
    assert raised.value.violation.code == "AS006"


def test_agent_session_provider_contexts_are_isolated(kora_capture: Capture) -> None:
    subject = provider()
    supplied = context_for(kora_capture)
    first = dataclasses.replace(
        supplied,
        namespace_id="first",
        logger=supplied.logger.bind(namespace_id="first"),
    )
    second = dataclasses.replace(
        supplied,
        namespace_id="second",
        logger=supplied.logger.bind(namespace_id="second"),
    )

    assert subject.validate(payload(), first) == []
    assert subject.summary(payload(), second) == expected_summary()
    assert subject.digest_fields(payload(), first) == [""]
    assert subject.validate({"private": INVALID_MARKER}, second)
    records = kora_capture.finish()

    assert len(records) == 4
    assert all(marker not in json.dumps(records) for marker in MARKERS)
    assert all(record["component"] == "conformance" for record in records)
    assert [record["namespace_id"] for record in records] == [
        "first",
        "second",
        "first",
        "second",
    ]
    assert not any(
        isinstance(value, ProtocolContext) for value in vars(subject).values()
    )


def test_agent_session_provider_diagnostics_are_content_free(
    kora_capture: Capture,
) -> None:
    subject = provider()
    context = context_for(kora_capture)
    value = payload()

    subject.validate(value, context)
    subject.summary(value, context)
    subject.digest_fields(value, context)
    subject.validate({"unknown": INVALID_MARKER}, context)
    records = kora_capture.finish()

    assert len(records) == 4
    for record in records:
        assert set(record) == {
            "schema_version",
            "timestamp",
            "level",
            "event",
            "message",
            "component",
            "logger",
            "instance_id",
            "operation",
            "outcome",
            "reason",
            "violations",
        }
        assert record["level"] == "DEBUG"
        assert record["component"] == "conformance"
        assert record["logger"] == "kora.conformance"
        assert record["event"] == "artifact_type.agent_session.outcome"
        assert record["message"] == "an artifact type operation completed"
        assert type(record["violations"]) is int
    assert [record["operation"] for record in records] == [
        "validate",
        "summary",
        "digest_fields",
        "validate",
    ]
    assert [record["outcome"] for record in records] == [
        "accepted",
        "accepted",
        "accepted",
        "refused",
    ]
    assert [record["reason"] for record in records] == ["valid"] * 3 + ["AS003"]
    assert [record["violations"] for record in records] == [0, 0, 0, 1]
    assert all(marker not in json.dumps(records) for marker in MARKERS)


def test_agent_session_provider_failures_are_content_free(
    kora_capture: Capture,
) -> None:
    subject = provider()
    value: JsonObject = {"private-marker": INVALID_MARKER}

    violations = subject.validate(value, context_for(kora_capture))

    assert [item.model_dump() for item in violations] == [
        {
            "code": "AS003",
            "message": "AgentSession value is outside its revision domain.",
            "path": (),
            "location": None,
            "offset": None,
            "node": None,
            "hint": None,
        }
    ]
    assert all(
        marker not in json.dumps([v.model_dump() for v in violations])
        for marker in MARKERS
    )


def test_agent_session_provider_entry_point(kora_capture: Capture) -> None:
    metadata = load_provider(
        ProtocolName.ARTIFACT_TYPE, "agent_session", kora_capture.diagnostics
    )

    created = metadata.implementation({}, binding())

    assert type(created) is AgentSessionArtifactType
    assert metadata.name == "agent_session"
    assert metadata.version == "1"
    assert created.name == "AgentSession"
    assert created.parent is None


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_agent_session_provider_ignores_blocked_or_broken_diagnostics(
    sink: str,
) -> None:
    arguments = ["-m", "tests.plugins.artifact_type.agent_session_sink_probe", sink]

    process = run_python(arguments, {})

    assert process.code == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["completed"] == 150
    assert process.stderr == b""
    assert "private-sink-marker" not in process.stdout.decode()
    assert all(marker not in process.stdout.decode() for marker in MARKERS)
    if sink == "blocked":
        assert not report["drained"]
        assert report["dropped"] > 0
    else:
        assert report["drained"]
        assert report["sink_failed"]


def test_agent_session_artifact_type_provider_lifetime(kora_capture: Capture) -> None:
    subject = provider()
    context = context_for(kora_capture)
    workloads = lifetime_shapes()
    initial = {name: id(value) for name, value in vars(subject).items()}
    expected_state = {
        "_schema": binding().schema,
        "_revision_descriptor": binding().revision_descriptor,
        "config_schema": {"type": "object", "additionalProperties": False},
    }
    assert set(initial) == set(expected_state)
    assert vars(subject) == expected_state

    for index in range(100):
        value, invalid = workloads[index % len(workloads)]
        assert subject.validate(value, context) == []
        summary = subject.summary(value, context)
        assert summary == summarize_agent_session(parse_agent_session(value))
        assert summary is not value
        assert subject.digest_fields(value, context) == [""]
        assert subject.validate(invalid, context)
        assert {name: id(value) for name, value in vars(subject).items()} == initial
        assert vars(subject) == expected_state
        del summary
    kora_capture.finish()
    state = kora_capture.diagnostics.snapshot()

    assert state.queue_records == state.queue_bytes == state.in_flight_bytes == 0
    assert vars(subject) == expected_state
    assert_capture_released(kora_capture)
