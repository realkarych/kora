import dataclasses
import json
from collections.abc import Callable
from typing import cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref import (
    VcsRefValidationError,
    canonical_vcs_ref,
    parse_vcs_ref,
    parse_vcs_ref_json,
    summarize_vcs_ref,
    vcs_ref_digest,
)
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.ir.vcs_ref_cases import ARTIFACTS, exact_size, wire
from tests.plugins.artifact_type.support import binding, payload
from tests.support.observability import run_python


def provider() -> VcsRefArtifactType:
    return VcsRefArtifactType({}, binding())


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
def test_vcs_ref_provider_refuses_incompatible_binding(path: tuple[str, ...]) -> None:
    supplied = changed_descriptor(path, "private-binding-marker")

    with pytest.raises(KoraProtocolError) as raised:
        VcsRefArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert "private-binding-marker" not in str(raised.value)
    assert "private-binding-marker" not in json.dumps(raised.value.details)
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None


@pytest.mark.parametrize("field", ["name", "parent", "schema", "revision_descriptor"])
def test_vcs_ref_provider_requires_expanded_binding(field: str) -> None:
    original = binding()
    replacement: object = (
        {"builtin_revision": "1"}
        if field in {"schema", "revision_descriptor"}
        else "other"
    )
    supplied = dataclasses.replace(original, **{field: replacement})

    with pytest.raises(KoraProtocolError) as raised:
        VcsRefArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("field", ["schema", "revision_descriptor"])
@pytest.mark.parametrize("shape", ["wide", "deep", "cyclic"])
def test_vcs_ref_provider_refuses_wide_and_deep_bindings_before_copy(
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
        VcsRefArtifactType({}, invalid)

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
def test_vcs_ref_provider_preserves_exact_schema_scalar_types(
    path: tuple[str, ...],
    replacement: JsonValue,
) -> None:
    supplied = binding()
    target = supplied.schema
    for name in path[:-1]:
        target = nested(target, name)
    target[path[-1]] = replacement

    with pytest.raises(KoraProtocolError) as raised:
        VcsRefArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize("keyword", ["title", "x-private-annotation"])
def test_vcs_ref_provider_compares_schema_annotations(keyword: str) -> None:
    supplied = binding()
    supplied.schema[keyword] = "private-schema-marker"

    with pytest.raises(KoraProtocolError) as raised:
        VcsRefArtifactType({}, supplied)

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert (
        supplied.revision_descriptor["schema_sha256"]
        == binding().revision_descriptor["schema_sha256"]
    )


def test_vcs_ref_provider_owns_bound_contract(kora_capture: Capture) -> None:
    supplied = binding()
    subject = VcsRefArtifactType({}, supplied)
    expected = provider().schema

    nested(nested(supplied.schema, "properties"), "repo")["minLength"] = 0
    nested(supplied.revision_descriptor, "behavior")["summary"] = "changed"
    view = subject.schema
    view.clear()
    other = subject.schema
    nested(nested(other, "properties"), "repo")["minLength"] = 0

    assert subject.schema == expected
    assert provider().schema == expected
    assert subject.validate(payload(), context_for(kora_capture)) == []


def test_vcs_ref_provider_accepts_schema_member_order(kora_capture: Capture) -> None:
    supplied = binding()
    reordered = dataclasses.replace(
        supplied,
        schema=dict(reversed(list(supplied.schema.items()))),
        revision_descriptor=dict(reversed(list(supplied.revision_descriptor.items()))),
    )

    subject = VcsRefArtifactType({}, reordered)

    assert subject.schema == supplied.schema
    assert subject.validate(payload(), context_for(kora_capture)) == []


@pytest.mark.parametrize(
    "configuration", [{"unknown": "private-config-marker"}, {"x": None}]
)
def test_vcs_ref_provider_configuration_is_closed(configuration: JsonObject) -> None:
    supplied = binding()

    with pytest.raises(KoraProtocolError) as raised:
        VcsRefArtifactType(configuration, supplied)

    assert raised.value.code == "artifact_type_configuration_invalid"
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert "private-config-marker" not in str(raised.value)


def candidates() -> list[JsonValue]:
    values: list[JsonValue] = [payload(), None, [], 1, True, "private-payload-marker"]
    values.extend(
        cast("JsonValue", json.loads(path.read_bytes()))
        for path in sorted((ARTIFACTS / "examples" / "invalid").glob("*.json"))
    )
    values.extend(
        cast("JsonValue", exact_size(size)) for size in (65_536, 1_048_576, 1_048_577)
    )
    values.extend(
        [
            {**payload(), "commit_url": "https://[::1]suffix/"},
            {**payload(), "changed_paths": ["same", "same"]},
            {**payload(), "url": "https://example.test/"},
        ]
    )
    return values


@pytest.mark.parametrize("candidate", candidates())
def test_vcs_ref_provider_matches_model_validation(
    kora_capture: Capture, candidate: JsonValue
) -> None:
    subject = provider()
    try:
        parse_vcs_ref(candidate)
    except VcsRefValidationError as error:
        expected = [error.violation]
    else:
        expected = []

    result = subject.validate(candidate, context_for(kora_capture))

    assert result == expected
    assert len(result) <= 1


def test_vcs_ref_provider_digest_selection_is_stable(kora_capture: Capture) -> None:
    subject = provider()
    value = payload()
    context = context_for(kora_capture)

    first = subject.digest_fields(value, context)
    first.append("/substituted")
    second = subject.digest_fields(dict(reversed(list(value.items()))), context)

    assert second == [""]
    assert canonical_vcs_ref(parse_vcs_ref(value)) == wire(value)
    assert vcs_ref_digest(parse_vcs_ref(value)) == vcs_ref_digest(
        parse_vcs_ref(dict(reversed(list(value.items()))))
    )


def test_vcs_ref_provider_summary_is_detached(kora_capture: Capture) -> None:
    subject = provider()
    value = payload()
    expected = summarize_vcs_ref(parse_vcs_ref(value))
    context = context_for(kora_capture)

    first = cast("JsonObject", subject.summary(value, context))
    nested(first, "pushed_by")["display_name"] = "changed-result"
    cast("list[JsonValue]", first["changed_paths"]).append("changed")
    nested(value, "pushed_by")["display_name"] = "changed-input"

    assert subject.summary(payload(), context) == expected
    assert first != expected


@pytest.mark.parametrize("hook", ["summary", "digest_fields"])
def test_vcs_ref_provider_extraction_requires_valid_payload(
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


def test_vcs_ref_provider_preserves_admitted_raw_value(kora_capture: Capture) -> None:
    raw = wire(payload())
    admitted = summarize_vcs_ref(parse_vcs_ref_json(raw))
    subject = provider()

    projected = subject.summary(admitted, context_for(kora_capture))

    assert projected == admitted
    with pytest.raises(VcsRefValidationError):
        parse_vcs_ref_json(b'{"repo":"a","repo":"b"}')


def test_vcs_ref_provider_contexts_are_isolated(kora_capture: Capture) -> None:
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


def test_vcs_ref_provider_diagnostics_are_content_free(kora_capture: Capture) -> None:
    subject = provider()
    context = context_for(kora_capture)
    value = {**payload(), "diff_stat": "private-diff-marker"}

    subject.validate(value, context)
    subject.summary(value, context)
    subject.digest_fields(value, context)
    subject.validate({"unknown": "private-error-marker"}, context)
    records = kora_capture.finish()

    assert records
    assert all(record["level"] == "DEBUG" for record in records)
    assert "private-diff-marker" not in json.dumps(records)
    assert "private-error-marker" not in json.dumps(records)


def test_vcs_ref_provider_failures_are_content_free(kora_capture: Capture) -> None:
    subject = provider()

    violations = subject.validate(
        {"private-marker": "private-marker"}, context_for(kora_capture)
    )

    assert len(violations) == 1
    assert "private-marker" not in json.dumps(
        [item.model_dump() for item in violations]
    )


def test_vcs_ref_provider_entry_point(kora_capture: Capture) -> None:
    metadata = load_provider(
        ProtocolName.ARTIFACT_TYPE, "vcs_ref", kora_capture.diagnostics
    )

    created = metadata.implementation({}, binding())

    assert isinstance(created, VcsRefArtifactType)
    assert metadata.name == "vcs_ref"
    assert created.name == "VcsRef"


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_vcs_ref_provider_ignores_blocked_or_broken_diagnostics(sink: str) -> None:
    arguments = ["-m", "tests.plugins.artifact_type.sink_probe", sink]

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


def test_artifact_type_provider_lifetime(kora_capture: Capture) -> None:
    subject = provider()
    context = context_for(kora_capture)
    initial = {name: id(value) for name, value in vars(subject).items()}

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
