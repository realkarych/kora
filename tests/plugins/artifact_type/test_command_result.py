import base64
import dataclasses
import json
from collections.abc import Callable
from typing import cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import (
    CommandResultValidationError,
    command_result_digest,
    parse_command_result,
    parse_command_result_json,
    summarize_command_result,
    verify_command_result_captures,
)
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.ir.command_result_cases import (
    dense,
    fixture,
    fixture_bytes,
    rows,
    stream_bytes,
    with_paths,
    with_resources,
    with_stream,
)
from tests.plugins.artifact_type.command_result_support import MARKER, binding, payload
from tests.plugins.artifact_type.support import binding as vcs_ref_binding
from tests.support.observability import run_python


def provider() -> CommandResultArtifactType:
    return CommandResultArtifactType({}, binding())


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


@pytest.mark.parametrize("path", descriptor_paths(binding().revision_descriptor))
def test_command_result_provider_refuses_incompatible_binding(
    path: tuple[str, ...],
) -> None:
    supplied = changed_descriptor(path, "private-binding-marker")

    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert "private-binding-marker" not in str(raised.value)
    assert "private-binding-marker" not in json.dumps(raised.value.details)
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None


@pytest.mark.parametrize("field", ["name", "parent", "schema", "revision_descriptor"])
def test_command_result_provider_requires_expanded_binding(field: str) -> None:
    original = binding()
    replacement: object = (
        {"builtin_revision": "1"}
        if field in {"schema", "revision_descriptor"}
        else "other"
    )
    supplied = dataclasses.replace(original, **{field: replacement})

    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("supplied", [None, {}, "private-binding-marker", True])
def test_command_result_provider_requires_binding_type(supplied: object) -> None:
    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType({}, cast("BuiltinArtifactTypeBinding", supplied))

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert raised.value.details == {"rule": "binding"}
    assert raised.value.__context__ is raised.value.__cause__ is None


def test_command_result_provider_rejects_cross_type_binding() -> None:
    with pytest.raises(KoraProtocolError) as command_refusal:
        CommandResultArtifactType({}, vcs_ref_binding())
    with pytest.raises(KoraProtocolError) as vcs_ref_refusal:
        VcsRefArtifactType({}, binding())

    assert command_refusal.value.code == "artifact_type_binding_incompatible"
    assert vcs_ref_refusal.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("field", ["schema", "revision_descriptor"])
@pytest.mark.parametrize("shape", ["wide", "deep", "cyclic"])
def test_command_result_provider_refuses_wide_and_deep_bindings_before_copy(
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
        CommandResultArtifactType({}, invalid)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("properties", "repo", "minLength"), True),
        (("properties", "repo", "minLength"), 1.0),
        (("additionalProperties",), 0),
    ],
)
def test_command_result_provider_preserves_exact_binding_types(
    path: tuple[str, ...],
    replacement: JsonValue,
) -> None:
    supplied = binding()
    target = supplied.schema
    for name in path[:-1]:
        target = nested(target, name)
    target[path[-1]] = replacement

    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("replacement", [True, 4096.0])
def test_command_result_provider_preserves_descriptor_integer_types(
    replacement: JsonValue,
) -> None:
    supplied = changed_descriptor(("validation", "max_tail_bytes"), replacement)

    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("keyword", ["title", "x-private-annotation"])
def test_command_result_provider_compares_schema_annotations(keyword: str) -> None:
    supplied = binding()
    supplied.schema[keyword] = "private-schema-marker"

    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert (
        supplied.revision_descriptor["schema_sha256"]
        == binding().revision_descriptor["schema_sha256"]
    )


def test_command_result_provider_owns_bound_contract(kora_capture: Capture) -> None:
    supplied = binding()
    subject = CommandResultArtifactType({}, supplied)
    expected = provider().schema

    nested(nested(supplied.schema, "properties"), "repo")["minLength"] = 0
    nested(supplied.revision_descriptor, "behavior")["summary"] = "changed"
    view = subject.schema
    view.clear()
    other = subject.schema
    nested(nested(other, "properties"), "repo")["minLength"] = 0

    assert subject.schema == expected
    assert provider().schema == expected
    assert vars(subject)["_schema"] is not supplied.schema
    assert vars(subject)["_revision_descriptor"] is not supplied.revision_descriptor
    assert vars(subject)["_revision_descriptor"] == binding().revision_descriptor
    assert subject.validate(payload(), context_for(kora_capture)) == []


def test_command_result_provider_accepts_schema_member_order(
    kora_capture: Capture,
) -> None:
    supplied = binding()
    reordered = dataclasses.replace(
        supplied,
        schema=cast(
            "JsonObject",
            json.loads(json.dumps(dict(reversed(list(supplied.schema.items()))))),
        ),
        revision_descriptor=cast(
            "JsonObject",
            json.loads(
                json.dumps(dict(reversed(list(supplied.revision_descriptor.items()))))
            ),
        ),
    )

    subject = CommandResultArtifactType({}, reordered)

    assert subject.schema == supplied.schema
    assert subject.validate(payload(), context_for(kora_capture)) == []


@pytest.mark.parametrize(
    "configuration",
    [{"unknown": "private-config-marker"}, {"x": None}, [], None, False],
)
def test_command_result_provider_configuration_is_closed(configuration: object) -> None:
    supplied = binding()

    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType(cast("JsonObject", configuration), supplied)

    assert raised.value.code == "artifact_type_configuration_invalid"
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert "private-config-marker" not in str(raised.value)
    assert "private-config-marker" not in json.dumps(raised.value.details)


def candidates() -> list[JsonValue]:
    values: list[JsonValue] = [payload(), None, [], 1, True, "private-payload-marker"]
    values.extend(
        fixture(cast("str", case["name"])) for case in rows("canonical_cases")
    )
    values.extend(cast("JsonValue", case["value"]) for case in rows("semantic_cases"))
    for field in ("duration_ms", "exit_code", "signal"):
        for invalid in (True, 1.0, "1", 2**63, -(2**63)):
            values.append({**fixture(), field: invalid})
    for field, invalid in (
        ("size", 67108865),
        ("observed_bytes", -1),
        ("observed_bytes", 2**63),
        ("eof", 1),
        ("truncated", "false"),
        ("storage_ref", "a b"),
        ("tail_base64", "YR=="),
        ("tail_base64", "YQ"),
    ):
        values.append({**fixture(), "stdout": {**stream_bytes(b"a"), field: invalid}})
    values.extend(
        [
            dense(),
            {**fixture(), "duration_ms": 2**63 - 1},
            {**fixture(), "exit_code": 255},
            {**fixture(), "exit_code": -255, "signal": 255},
            {**fixture(), "exit_code": -256, "signal": 256},
            {**fixture(), "termination": "interrupted"},
            {**fixture(), "resource_protection": "protected"},
            {**fixture(), "repo": "a" * 4097},
            {**fixture(), "commit": "a" * 256},
            with_resources([f"a{index}" for index in range(65)]),
            with_resources(["a"], 262145),
            with_resources(["a" * 256]),
            with_paths(["a"] * 65),
            with_paths(["a" * 4097]),
            with_paths(["/".join(["a"] * 33)]),
            {**fixture(), "stdout": stream_bytes(b"", observed=2**63 - 1, eof=False)},
            {
                **fixture(),
                "stdout": {
                    **stream_bytes(b"a"),
                    "payload_digest": "sha256:" + "0" * 64,
                },
            },
        ]
    )
    return values


@pytest.mark.parametrize("candidate", candidates())
def test_command_result_provider_matches_model_validation(
    kora_capture: Capture, candidate: JsonValue
) -> None:
    subject = provider()
    try:
        parse_command_result(candidate)
    except CommandResultValidationError as error:
        expected = [error.violation]
    else:
        expected = []

    result = subject.validate(candidate, context_for(kora_capture))

    assert result == expected
    assert len(result) <= 1


def test_command_result_provider_digest_selection_is_stable(
    kora_capture: Capture,
) -> None:
    subject = provider()
    value = payload()
    context = context_for(kora_capture)

    first = subject.digest_fields(value, context)
    first.append("/substituted")
    second = subject.digest_fields(dict(reversed(list(value.items()))), context)

    assert second == [""]
    assert command_result_digest(parse_command_result(value)) == command_result_digest(
        parse_command_result(dict(reversed(list(value.items()))))
    )


def test_command_result_provider_summary_is_detached(kora_capture: Capture) -> None:
    subject = provider()
    value = payload()
    expected = summarize_command_result(parse_command_result(value))
    context = context_for(kora_capture)

    first = cast("JsonObject", subject.summary(value, context))
    nested(first, "stdout")["storage_ref"] = "changed-result"
    cast("list[JsonValue]", first["protected_paths"]).append("changed")
    cast("JsonObject", cast("list[JsonValue]", first["resources"])[0])["size"] = 1
    nested(value, "stderr")["storage_ref"] = "changed-input"

    assert subject.summary(payload(), context) == expected
    assert first != expected
    assert nested(value, "stdout")["storage_ref"] == MARKER


@pytest.mark.parametrize("hook", ["summary", "digest_fields"])
def test_command_result_provider_extraction_requires_valid_payload(
    kora_capture: Capture, hook: str
) -> None:
    subject = provider()
    action = cast("Callable[[JsonValue, object], object]", getattr(subject, hook))

    with pytest.raises(KoraProtocolError) as raised:
        action({"private": "private-payload-marker"}, context_for(kora_capture))

    assert raised.value.code == "artifact_type_payload_invalid"
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert "private-payload-marker" not in str(raised.value)
    assert "private-payload-marker" not in json.dumps(raised.value.details)


def test_command_result_provider_preserves_admitted_raw_value(
    kora_capture: Capture,
) -> None:
    raw = fixture_bytes()
    admitted = summarize_command_result(parse_command_result_json(raw))
    subject = provider()

    projected = subject.summary(admitted, context_for(kora_capture))

    assert projected == admitted
    with pytest.raises(CommandResultValidationError):
        parse_command_result_json(raw[:-1] + b',"exit_code":1}')


def test_command_result_provider_keeps_capture_integrity_with_byte_owner(
    kora_capture: Capture,
) -> None:
    retained = b"a" * 4097
    candidate = with_stream(retained)
    nested(candidate, "stdout")["payload_digest"] = "sha256:" + "0" * 64
    subject = provider()
    value = parse_command_result(candidate)

    violations = subject.validate(candidate, context_for(kora_capture))
    with pytest.raises(CommandResultValidationError) as raised:
        verify_command_result_captures(value, stdout=retained, stderr=b"")

    assert violations == []
    assert (
        nested(candidate, "stdout")["tail_base64"]
        == base64.b64encode(retained[-4096:]).decode()
    )
    assert raised.value.violation.code == "CR006"


def test_command_result_provider_contexts_are_isolated(kora_capture: Capture) -> None:
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
    assert subject.validate({"private": "payload-secret"}, second)
    records = kora_capture.finish()

    assert len(records) == 2
    assert "payload-secret" not in json.dumps(records)
    assert all(record["component"] == "conformance" for record in records)
    assert [record["namespace_id"] for record in records] == ["first", "second"]
    assert not any(isinstance(value, type(first)) for value in vars(subject).values())


def test_command_result_provider_diagnostics_are_content_free(
    kora_capture: Capture,
) -> None:
    subject = provider()
    context = context_for(kora_capture)
    value = payload()

    subject.validate(value, context)
    subject.summary(value, context)
    subject.digest_fields(value, context)
    subject.validate({"unknown": "private-error-marker"}, context)
    records = kora_capture.finish()

    assert records
    assert len(records) == 4
    assert all(record["level"] == "DEBUG" for record in records)
    assert all(
        record["event"] == "artifact_type.command_result.outcome" for record in records
    )
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
    assert [record["reason"] for record in records] == [
        "valid",
        "valid",
        "valid",
        "CR003",
    ]
    assert [record["violations"] for record in records] == [0, 0, 0, 1]
    assert MARKER not in json.dumps(records)
    assert base64.b64encode(MARKER.encode()).decode() not in json.dumps(records)
    assert "private-error-marker" not in json.dumps(records)


def test_command_result_provider_failures_are_content_free(
    kora_capture: Capture,
) -> None:
    subject = provider()

    violations = subject.validate({"private-marker": MARKER}, context_for(kora_capture))

    assert len(violations) == 1
    assert "private-marker" not in json.dumps(
        [item.model_dump() for item in violations]
    )
    assert MARKER not in json.dumps([item.model_dump() for item in violations])


def test_command_result_provider_entry_point(kora_capture: Capture) -> None:
    metadata = load_provider(
        ProtocolName.ARTIFACT_TYPE, "command_result", kora_capture.diagnostics
    )

    created = metadata.implementation({}, binding())

    assert isinstance(created, CommandResultArtifactType)
    assert metadata.name == "command_result"
    assert metadata.version == "1"
    assert created.name == "CommandResult"
    assert created.parent is None


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_command_result_provider_ignores_blocked_or_broken_diagnostics(
    sink: str,
) -> None:
    arguments = ["-m", "tests.plugins.artifact_type.command_result_sink_probe", sink]

    process = run_python(arguments, {})

    assert process.code == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["completed"] == 150
    assert process.stderr == b""
    assert "private-sink-marker" not in process.stdout.decode()
    if sink == "blocked":
        assert not report["drained"]
        assert report["dropped"] > 0
    else:
        assert report["drained"]
        assert report["sink_failed"]


def test_command_result_artifact_type_provider_lifetime(kora_capture: Capture) -> None:
    subject = provider()
    context = context_for(kora_capture)
    initial = {name: id(value) for name, value in vars(subject).items()}
    assert set(initial) == {"_schema", "_revision_descriptor", "config_schema"}

    for _ in range(100):
        value = payload()
        assert subject.validate(value, context) == []
        summary = subject.summary(value, context)
        assert summary == value
        assert summary is not value
        assert subject.digest_fields(value, context) == [""]
        assert subject.validate({"invalid": "private-lifetime-marker"}, context)
        assert {name: id(value) for name, value in vars(subject).items()} == initial
    kora_capture.finish()

    assert_capture_released(kora_capture)
