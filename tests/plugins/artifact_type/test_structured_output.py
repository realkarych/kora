import dataclasses
import json
import math
import traceback
from collections import UserDict
from collections.abc import Callable
from decimal import Decimal
from typing import Never, cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir import (
    StructuredOutputValidationError,
    canonical_structured_output,
    parse_structured_output,
    parse_structured_output_json,
)
from kora.ir.data import detach_ir_data
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_json import MAX_INTEGER_BITS, MAX_JSON_BYTES, encode_snapshot
from kora.plugins.artifact_type.agent_session import AgentSessionArtifactType
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.plugins.artifact_type.structured_output import StructuredOutputArtifactType
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.ir.structured_output_cases import (
    CanonicalCase,
    canonical_cases,
    exact_string,
    forged,
    hostile_inputs,
    nested,
)
from tests.plugins.artifact_type.agent_session_support import binding as agent_binding
from tests.plugins.artifact_type.command_result_support import (
    binding as command_binding,
)
from tests.plugins.artifact_type.structured_output_support import (
    INVALID_MARKER,
    KEY_MARKER,
    MARKERS,
    ROOT_FACTORIES,
    binding,
    payload,
    rejected,
    reverse_members,
)
from tests.plugins.artifact_type.support import binding as vcs_binding
from tests.support.observability import run_python

Path = tuple[str | int, ...]


class ExternalBinding(BuiltinArtifactTypeBinding):
    pass


class ExternalString(str):
    pass


class ExternalInteger(int):
    pass


class ExternalFloat(float):
    pass


class ExternalDict(dict[str, JsonValue]):
    def __deepcopy__(self, memo: dict[int, object]) -> Never:
        raise AssertionError("an untrusted dictionary was copied")


class ExternalList(list[JsonValue]):
    pass


class HostileValue:
    def __deepcopy__(self, memo: dict[int, object]) -> Never:
        raise AssertionError("an untrusted value was copied")

    def __eq__(self, other: object) -> Never:
        raise AssertionError("untrusted equality was invoked")


def provider() -> StructuredOutputArtifactType:
    return StructuredOutputArtifactType({}, binding())


def at(value: JsonValue, path: Path) -> JsonValue:
    for key in path:
        if isinstance(key, str):
            assert isinstance(value, dict)
            value = value[key]
        else:
            assert isinstance(value, list)
            value = value[key]
    return value


def replace_at(value: JsonValue, path: Path, replacement: JsonValue) -> None:
    parent = at(value, path[:-1])
    key = path[-1]
    if isinstance(key, str):
        assert isinstance(parent, dict)
        parent[key] = replacement
    else:
        assert isinstance(parent, list)
        parent[key] = replacement


def paths(value: JsonValue, prefix: Path = ()) -> list[Path]:
    result = [prefix]
    if isinstance(value, dict):
        for name, item in value.items():
            result.extend(paths(item, (*prefix, name)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(paths(item, (*prefix, index)))
    return result


def binding_paths() -> list[tuple[str, Path]]:
    supplied = binding()
    return [
        (field, path)
        for field, value in (
            ("schema", supplied.schema),
            ("revision_descriptor", supplied.revision_descriptor),
        )
        for path in paths(value)
    ]


def assert_binding_refused(supplied: object, rule: str) -> None:
    with pytest.raises(KoraProtocolError) as raised:
        StructuredOutputArtifactType({}, cast("BuiltinArtifactTypeBinding", supplied))

    assert raised.value.code == "artifact_type_binding_incompatible"
    assert raised.value.message == "the artifact type binding is incompatible"
    assert raised.value.details == {"rule": rule}
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert INVALID_MARKER not in str(raised.value)
    assert INVALID_MARKER not in json.dumps(raised.value.details)


def assert_native_identity(actual: JsonValue, expected: JsonValue) -> None:
    assert type(actual) is type(expected)
    assert encode_snapshot(actual) == encode_snapshot(expected)
    if isinstance(expected, float):
        assert isinstance(actual, float)
        assert math.copysign(1.0, actual) == math.copysign(1.0, expected)
    elif isinstance(expected, dict):
        assert isinstance(actual, dict)
        assert actual.keys() == expected.keys()
        for key, item in expected.items():
            assert_native_identity(actual[key], item)
    elif isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected)
        for first, second in zip(actual, expected, strict=True):
            assert_native_identity(first, second)


@pytest.mark.parametrize("build", ROOT_FACTORIES)
def test_structured_output_provider_accepts_all_json_roots(
    kora_capture: Capture, build: Callable[[], JsonValue]
) -> None:
    subject = provider()
    value = build()
    before = encode_snapshot(value)
    context = context_for(kora_capture)

    result = subject.validate(value, context)
    selected = subject.digest_fields(value, context)
    summary = subject.summary(value, context)

    assert result == []
    assert selected == [""]
    assert_native_identity(summary, value)
    assert encode_snapshot(value) == before


@pytest.mark.parametrize("case", canonical_cases(), ids=lambda case: case.name)
def test_structured_output_provider_preserves_canonical_identity(
    kora_capture: Capture, case: CanonicalCase
) -> None:
    subject = provider()
    value = parse_structured_output_json(case.raw).value
    context = context_for(kora_capture)

    result = subject.summary(value, context)

    assert subject.validate(value, context) == []
    assert subject.digest_fields(value, context) == [""]
    assert_native_identity(result, value)
    assert (
        canonical_structured_output(parse_structured_output(result)) == case.canonical
    )


@pytest.mark.parametrize(("field", "path"), binding_paths())
def test_structured_output_provider_requires_exact_expanded_binding(
    field: str, path: Path
) -> None:
    supplied = binding()
    value = supplied.schema if field == "schema" else supplied.revision_descriptor
    original = at(value, path)
    replacement: JsonValue = (
        ExternalDict(original)
        if isinstance(original, dict)
        else ExternalList(original)
        if isinstance(original, list)
        else ExternalString(original)
        if isinstance(original, str)
        else float(original)
        if type(original) is int
        else False
    )
    if path:
        replace_at(value, path, replacement)
    else:
        supplied = dataclasses.replace(supplied, **{field: replacement})

    assert_binding_refused(supplied, field)


@pytest.mark.parametrize(("field", "path"), binding_paths())
def test_structured_output_provider_compares_complete_binding(
    field: str, path: Path
) -> None:
    supplied = binding()
    value = supplied.schema if field == "schema" else supplied.revision_descriptor
    original = at(value, path)
    if isinstance(original, dict):
        original[INVALID_MARKER] = None
    elif isinstance(original, list):
        original.append(INVALID_MARKER)
    else:
        replace_at(value, path, INVALID_MARKER)

    assert_binding_refused(supplied, field)


@pytest.mark.parametrize(("field", "path"), binding_paths())
def test_structured_output_provider_refuses_missing_binding_members(
    field: str, path: Path
) -> None:
    supplied = binding()
    value = supplied.schema if field == "schema" else supplied.revision_descriptor
    target = at(value, path)
    if isinstance(target, dict):
        target.pop(next(iter(target)))
    elif isinstance(target, list):
        target.pop()
    else:
        parent = at(value, path[:-1])
        key = path[-1]
        if isinstance(key, str):
            assert isinstance(parent, dict)
            parent.pop(key)
        else:
            assert isinstance(parent, list)
            parent.pop(key)

    assert_binding_refused(supplied, field)


@pytest.mark.parametrize(
    ("field", "replacement", "rule"),
    [
        ("name", "StructuredOutputChild", "name"),
        ("name", "List[StructuredOutput]", "name"),
        ("name", ExternalString("StructuredOutput"), "name"),
        ("parent", "StructuredOutput", "parent"),
        ("parent", False, "parent"),
        ("schema", {"builtin_revision": "1"}, "schema"),
        ("revision_descriptor", {"builtin_revision": "1"}, "revision_descriptor"),
    ],
)
def test_structured_output_provider_refuses_substituted_binding(
    field: str, replacement: object, rule: str
) -> None:
    supplied = dataclasses.replace(binding(), **{field: replacement})

    assert_binding_refused(supplied, rule)


@pytest.mark.parametrize(
    "supplied", [None, {}, INVALID_MARKER, True, ExternalBinding(**vars(binding()))]
)
def test_structured_output_provider_requires_binding_type(supplied: object) -> None:
    assert_binding_refused(supplied, "binding")


@pytest.mark.parametrize("field", ["schema", "revision_descriptor"])
@pytest.mark.parametrize("shape", ["wide", "deep", "cyclic", "hostile"])
def test_structured_output_provider_refuses_hostile_binding_before_copy(
    field: str, shape: str
) -> None:
    supplied = binding()
    value: JsonObject = {}
    if shape == "wide":
        value = {f"unexpected{index}": None for index in range(20_000)}
    elif shape == "deep":
        for _ in range(2000):
            value = {"unexpected": value}
    elif shape == "cyclic":
        value["unexpected"] = value
    else:
        value = ExternalDict({"unexpected": cast("JsonValue", HostileValue())})
    target = supplied.schema if field == "schema" else supplied.revision_descriptor
    name = "type" if field == "schema" else "behavior"
    target[name] = value

    assert_binding_refused(supplied, field)


@pytest.mark.parametrize("other", [vcs_binding, command_binding, agent_binding])
def test_structured_output_provider_refuses_cross_type_binding(
    other: Callable[[], BuiltinArtifactTypeBinding],
) -> None:
    assert_binding_refused(other(), "name")
    for constructor in (
        VcsRefArtifactType,
        CommandResultArtifactType,
        AgentSessionArtifactType,
    ):
        with pytest.raises(KoraProtocolError) as raised:
            constructor({}, binding())
        assert raised.value.code == "artifact_type_binding_incompatible"


@pytest.mark.parametrize(
    "keyword", ["title", "default", "x-private-annotation", "allOf"]
)
def test_structured_output_provider_refuses_added_schema_constraints(
    keyword: str,
) -> None:
    supplied = binding()
    supplied.schema[keyword] = INVALID_MARKER

    assert_binding_refused(supplied, "schema")


@pytest.mark.parametrize("field", ["schema", "revision_descriptor"])
def test_structured_output_provider_refuses_non_native_member_names(field: str) -> None:
    supplied = binding()
    value = supplied.schema if field == "schema" else supplied.revision_descriptor
    key = next(iter(value))
    item = value.pop(key)
    value[ExternalString(key)] = item

    assert_binding_refused(supplied, field)


@pytest.mark.parametrize(
    "name",
    [
        "max_raw_json_bytes",
        "max_canonical_json_bytes",
        "max_json_depth",
        "max_integer_bits",
    ],
)
@pytest.mark.parametrize("kind", ["boolean", "integer_subclass"])
def test_structured_output_provider_requires_native_descriptor_integers(
    name: str, kind: str
) -> None:
    supplied = binding()
    limits = supplied.revision_descriptor["validation"]
    assert isinstance(limits, dict)
    original = limits[name]
    assert isinstance(original, int)
    limits[name] = True if kind == "boolean" else ExternalInteger(original)

    assert_binding_refused(supplied, "revision_descriptor")


@pytest.mark.parametrize(
    "configuration", [{"private": INVALID_MARKER}, [], None, False, ExternalDict()]
)
def test_structured_output_provider_configuration_is_closed(
    configuration: object,
) -> None:
    supplied = binding()

    with pytest.raises(KoraProtocolError) as raised:
        StructuredOutputArtifactType(cast("JsonObject", configuration), supplied)

    assert raised.value.code == "artifact_type_configuration_invalid"
    assert raised.value.message == "the artifact type configuration is invalid"
    assert raised.value.details == {"rule": "closed_empty_object"}
    assert not raised.value.retryable
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert INVALID_MARKER not in str(raised.value)


def test_structured_output_provider_does_not_alias_binding_or_summary(
    kora_capture: Capture,
) -> None:
    supplied = binding()
    configuration: JsonObject = {}
    subject = StructuredOutputArtifactType(configuration, supplied)
    fixed = binding()
    value = payload()
    context = context_for(kora_capture)
    before = encode_snapshot(value)

    summary = subject.summary(value, context)
    assert isinstance(summary, dict)
    items = summary[KEY_MARKER]
    assert isinstance(items, list)
    items.clear()
    assert encode_snapshot(value) == before
    view = subject.schema
    root_types = view["type"]
    assert isinstance(root_types, list)
    root_types.clear()
    subject.schema.clear()
    configuration[INVALID_MARKER] = None
    supplied.schema.clear()
    supplied.revision_descriptor.clear()
    value.clear()

    assert encode_snapshot(subject.summary(payload(), context)) == before
    assert summary[KEY_MARKER] == []
    assert summary["a"] == {"x": True}
    assert subject.schema == fixed.schema
    assert vars(subject)["_schema"] is not supplied.schema
    assert vars(subject)["_revision_descriptor"] is not supplied.revision_descriptor
    assert vars(subject)["_revision_descriptor"] == fixed.revision_descriptor
    assert subject.config_schema == {"type": "object", "additionalProperties": False}
    other = provider()
    subject.config_schema.clear()
    assert other.config_schema == {"type": "object", "additionalProperties": False}


def test_structured_output_provider_accepts_member_order(kora_capture: Capture) -> None:
    supplied = binding()
    reordered = dataclasses.replace(
        supplied,
        schema=cast("JsonObject", reverse_members(supplied.schema)),
        revision_descriptor=cast(
            "JsonObject", reverse_members(supplied.revision_descriptor)
        ),
    )
    subject = StructuredOutputArtifactType({}, reordered)
    context = context_for(kora_capture)
    first = subject.digest_fields(payload(), context)

    first.clear()
    second = subject.digest_fields(reverse_members(payload()), context)

    assert second == [""]
    assert first is not second
    assert subject.schema == supplied.schema


def native_refusals() -> list[object]:
    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    cyclic_dict: dict[str, object] = {}
    cyclic_dict[INVALID_MARKER] = cyclic_dict
    return [
        ExternalString(INVALID_MARKER),
        ExternalInteger(1),
        ExternalFloat(1.0),
        ExternalDict({INVALID_MARKER: None}),
        ExternalList([None]),
        HostileValue(),
        UserDict({INVALID_MARKER: None}),
        Decimal("1"),
        (INVALID_MARKER,),
        {1: INVALID_MARKER},
        {ExternalString(INVALID_MARKER): 1},
        float("nan"),
        float("inf"),
        -float("inf"),
        "\ud800",
        {"\udfff": None},
        b"null",
        cyclic_list,
        cyclic_dict,
        nested(17),
        1 << MAX_INTEGER_BITS,
        -(1 << MAX_INTEGER_BITS),
        exact_string(MAX_JSON_BYTES + 1),
        forged(b"1.00"),
        forged(INVALID_MARKER),
    ]


@pytest.mark.parametrize("candidate", native_refusals())
def test_structured_output_provider_matches_native_refusals(
    kora_capture: Capture, candidate: object
) -> None:
    subject = provider()
    with pytest.raises(StructuredOutputValidationError) as refused:
        parse_structured_output(candidate)

    result = subject.validate(cast("JsonValue", candidate), context_for(kora_capture))

    assert result == [refused.value.violation]
    assert len(result) == 1


@pytest.mark.parametrize("name", list(hostile_inputs()))
def test_structured_output_provider_bounds_native_input(
    kora_capture: Capture, name: str
) -> None:
    subject = provider()
    value = hostile_inputs()[name]()
    with pytest.raises(StructuredOutputValidationError) as refused:
        parse_structured_output(value)

    result = subject.validate(cast("JsonValue", value), context_for(kora_capture))

    assert result == [refused.value.violation]


@pytest.mark.parametrize("candidate", native_refusals())
def test_structured_output_provider_refuses_invalid_extraction(
    kora_capture: Capture, candidate: object
) -> None:
    subject = provider()
    context = context_for(kora_capture)
    violations = subject.validate(cast("JsonValue", candidate), context)

    for method in (subject.summary, subject.digest_fields):
        with pytest.raises(KoraProtocolError) as raised:
            method(cast("JsonValue", candidate), context)

        assert raised.value.code == "artifact_type_payload_invalid"
        assert raised.value.message == "the artifact type payload is invalid"
        assert raised.value.details == {"violation": violations[0].code}
        assert not raised.value.retryable
        assert raised.value.__context__ is raised.value.__cause__ is None
        rendered = "".join(traceback.format_exception(raised.value))
        assert all(marker not in rendered for marker in MARKERS)


@pytest.mark.parametrize("size", [1024, 65_536, MAX_JSON_BYTES - 1, MAX_JSON_BYTES])
def test_structured_output_provider_accepts_frozen_byte_boundary(
    kora_capture: Capture, size: int
) -> None:
    subject = provider()
    value = exact_string(size)
    context = context_for(kora_capture)

    summary = subject.summary(value, context)

    assert subject.validate(value, context) == []
    assert len(encode_snapshot(summary)) == size


@pytest.mark.parametrize("sign", [-1, 1])
def test_structured_output_provider_accepts_frozen_depth_and_integer_limits(
    kora_capture: Capture, sign: int
) -> None:
    subject = provider()
    context = context_for(kora_capture)
    integer = sign * ((1 << MAX_INTEGER_BITS) - 1)
    deep = cast("JsonValue", nested(16))

    summary = subject.summary(integer, context)

    assert type(summary) is int
    assert summary == integer
    assert subject.validate(deep, context) == []
    assert_native_identity(subject.summary(deep, context), deep)


def test_structured_output_provider_errors_and_logs_are_content_free(
    kora_capture: Capture,
) -> None:
    subject = provider()
    context = context_for(kora_capture)

    subject.validate(payload(), context)
    subject.summary(payload(), context)
    subject.digest_fields(payload(), context)
    violations = subject.validate(rejected(INVALID_MARKER), context)
    records = kora_capture.finish()

    assert [item.model_dump() for item in violations] == [
        {
            "code": "SO003",
            "message": "StructuredOutput value is outside its JSON domain.",
            "path": (),
            "location": None,
            "offset": None,
            "node": None,
            "hint": None,
        }
    ]
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
        assert record["event"] == "artifact_type.structured_output.outcome"
        assert record["message"] == "an artifact type operation completed"
        assert type(record["violations"]) is int
    assert [record["operation"] for record in records] == [
        "validate",
        "summary",
        "digest_fields",
        "validate",
    ]
    assert [record["outcome"] for record in records] == ["accepted"] * 3 + ["refused"]
    assert [record["reason"] for record in records] == ["valid"] * 3 + ["SO003"]
    assert [record["violations"] for record in records] == [0, 0, 0, 1]
    assert all(marker not in json.dumps(records) for marker in MARKERS)


def test_structured_output_provider_contexts_are_independent(
    kora_capture: Capture,
) -> None:
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
    assert subject.summary(None, second) is None
    assert subject.digest_fields(payload(), first) == [""]
    assert subject.validate(rejected(INVALID_MARKER), second)
    records = kora_capture.finish()

    assert [record["namespace_id"] for record in records] == [
        "first",
        "second",
        "first",
        "second",
    ]
    assert all(marker not in json.dumps(records) for marker in MARKERS)
    assert not any(
        isinstance(value, ProtocolContext) for value in vars(subject).values()
    )


def test_structured_output_provider_entry_point(kora_capture: Capture) -> None:
    metadata = load_provider(
        ProtocolName.ARTIFACT_TYPE, "structured_output", kora_capture.diagnostics
    )

    created = metadata.implementation({}, binding())

    assert type(created) is StructuredOutputArtifactType
    assert metadata.name == "structured_output"
    assert metadata.version == "1"
    assert created.name == "StructuredOutput"
    assert created.parent is None


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_structured_output_provider_result_survives_unavailable_sink(sink: str) -> None:
    arguments = ["-m", "tests.plugins.artifact_type.structured_output_sink_probe", sink]

    process = run_python(arguments, {})

    assert process.code == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["completed"] == 150
    assert process.stderr == b""
    assert all(marker not in process.stdout.decode() for marker in MARKERS)
    if sink == "blocked":
        assert not report["drained"]
        assert report["dropped"] > 0
    else:
        assert report["drained"]
        assert report["sink_failed"]


def test_structured_output_provider_releases_per_call_state(
    kora_capture: Capture,
) -> None:
    subject = provider()
    context = context_for(kora_capture)
    value = payload()
    invalid = rejected(INVALID_MARKER)
    initial = {name: id(item) for name, item in vars(subject).items()}
    expected_state = {
        "_schema": binding().schema,
        "_revision_descriptor": binding().revision_descriptor,
        "config_schema": {"type": "object", "additionalProperties": False},
    }
    expected = detach_ir_data(value)
    assert set(initial) == set(expected_state)

    for _ in range(100):
        assert subject.validate(value, context) == []
        summary = subject.summary(value, context)
        assert_native_identity(summary, expected)
        assert summary is not value
        assert subject.digest_fields(value, context) == [""]
        assert subject.validate(invalid, context)
        assert {name: id(item) for name, item in vars(subject).items()} == initial
        assert vars(subject) == expected_state
        del summary
    kora_capture.finish()
    state = kora_capture.diagnostics.snapshot()

    assert state.queue_records == state.queue_bytes == state.in_flight_bytes == 0
    assert vars(subject) == expected_state
    assert_capture_released(kora_capture)
