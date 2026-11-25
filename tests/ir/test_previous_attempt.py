import json
import logging
from hashlib import sha256
from pathlib import Path
from traceback import format_exception, walk_tb
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry

from kora.db.identifiers import is_identifier
from kora.ir import (
    GraphDocument,
    PreviousAttempt,
    PreviousAttemptValidationError,
    validate_first_slice_configuration,
    validate_type_declarations,
)
from kora.ir.agent_session import parse_agent_session_json
from kora.ir.command_result import command_result_digest, parse_command_result_json
from kora.ir.json import JsonObject, JsonValue
from kora.ir.node import ErrorClass
from kora.ir.previous_attempt import (
    parse_previous_attempt_json,
    parse_retained_previous_attempt,
    previous_attempt_revision_descriptor,
    require_previous_attempt_revision,
)
from kora.ir.structured_json import MAX_JSON_BYTES, encode_snapshot, snapshot
from kora.ir.vcs_ref import parse_vcs_ref_json, vcs_ref_digest
from kora.protocols.runtime import ArtifactRef
from tests.ir.support import ROOT
from tests.ir.test_command_result_contract import SchemaClass, SchemaValidator
from tests.support.call_observation import ObservedCalls, observing_calls

ARTIFACTS = ROOT / "knowledge/specs/artifacts"
EXAMPLE = ROOT / "tests/fixtures/previous_attempt/graph.ir.json"
SCHEMA_HASH = "07d9e6e7e22334039584177116f56b8239c21a495633cf740caeda66097a7fd7"
DESCRIPTOR_HASH = "508aa8ecfdb57d2945b4007f59296c6128aa61d9aaa7c28286f8e57408d0e0e8"
VCS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
GATE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
LATER_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
SESSION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
MARKER = "private-feedback-marker"
MAX_ARTIFACTS = 17_771


def read_json(path: Path) -> JsonValue:
    return cast("JsonValue", json.loads(path.read_bytes()))


def reference(identity: str) -> JsonObject:
    return {"artifact_version_id": identity}


def evidence(
    artifacts: JsonObject | None = None,
    *,
    error_class: str = "attempt_failed",
    reason: str = MARKER,
    ordinal: int = 1,
) -> JsonObject:
    return {
        "artifacts": {} if artifacts is None else artifacts,
        "error_class": error_class,
        "reason": reason,
        "subgraph_attempt": ordinal,
    }


def vectors() -> list[JsonObject]:
    return cast(
        "list[JsonObject]", read_json(ARTIFACTS / "PreviousAttempt.vectors.json")
    )


@pytest.mark.parametrize("vector", vectors(), ids=lambda vector: str(vector["name"]))
def test_previous_attempt_canonical_vectors(vector: JsonObject) -> None:
    value = cast(JsonObject, vector["value"])
    canonical = str(vector["canonical"]).encode()
    digest = str(vector["payload_digest"])

    native = PreviousAttempt(value)
    wire = parse_previous_attempt_json(canonical)
    retained = parse_retained_previous_attempt(
        canonical, expected_payload_digest=digest
    )

    assert len(canonical) == vector["canonical_bytes"]
    assert digest == "sha256:" + sha256(canonical).hexdigest()
    assert all(
        model.canonical_bytes() == canonical for model in (native, wire, retained)
    )
    assert all(model.payload_digest() == digest for model in (native, wire, retained))
    assert all(model.value == value for model in (native, wire, retained))


def test_previous_attempt_digest_uses_every_member_and_ignores_key_order() -> None:
    outputs: JsonObject = {
        "implement.change": reference(VCS_ID),
        "check.result": reference(GATE_ID),
    }
    value = evidence(outputs)
    reordered = dict(reversed(tuple(value.items())))
    reordered["artifacts"] = dict(reversed(tuple(outputs.items())))
    changed_version = outputs.copy()
    changed_version["check.result"] = reference(LATER_ID)
    variants = (
        evidence(outputs, error_class="command_failed"),
        evidence(outputs, reason=MARKER + "?"),
        evidence(outputs, ordinal=2),
        evidence(
            {"other.change": reference(VCS_ID), "check.result": reference(GATE_ID)}
        ),
        evidence(changed_version),
    )

    digest = PreviousAttempt(value).payload_digest()
    changed = {PreviousAttempt(item).payload_digest() for item in variants}

    assert PreviousAttempt(reordered).payload_digest() == digest
    assert digest not in changed
    assert len(changed) == len(variants)


def test_previous_attempt_references_own_versions_with_equal_or_absent_content() -> (
    None
):
    examples = ARTIFACTS / "examples"
    vcs = parse_vcs_ref_json((examples / "vcs-ref.json").read_bytes())
    command = parse_command_result_json(
        (examples / "command-result/valid/nonzero-exit.json").read_bytes()
    )
    session = parse_agent_session_json(
        (examples / "agent-session/unavailable-no-usage.json").read_bytes()
    )
    change = ArtifactRef(
        artifact_version_id=VCS_ID,
        payload_digest="sha256:" + vcs_ref_digest(vcs),
        type_name="VcsRef",
    )
    command_digest = "sha256:" + command_result_digest(command)
    gate = ArtifactRef(
        artifact_version_id=GATE_ID,
        payload_digest=command_digest,
        type_name="CommandResult",
    )
    later = ArtifactRef(
        artifact_version_id=LATER_ID,
        payload_digest=command_digest,
        type_name="CommandResult",
    )
    by_name = {str(vector["name"]): vector for vector in vectors()}

    first = by_name["sql_failure"]
    second = by_name["same_command_bytes_different_version"]
    no_capture = by_name["accepted_session_without_capture"]
    first_value = PreviousAttempt(first["value"]).value
    second_value = PreviousAttempt(second["value"]).value
    session_value = PreviousAttempt(no_capture["value"]).value

    assert all(is_identifier(ref.artifact_version_id) for ref in (change, gate, later))
    assert is_identifier(SESSION_ID)
    assert first_value["artifacts"] == {
        "implement.change": reference(change.artifact_version_id),
        "check.result": reference(gate.artifact_version_id),
    }
    assert second_value["artifacts"] == {
        "implement.change": reference(change.artifact_version_id),
        "check.result": reference(later.artifact_version_id),
    }
    assert gate.payload_digest == later.payload_digest
    assert first["payload_digest"] != second["payload_digest"]
    assert session.value["transcript"] == {"capture": "unavailable"}
    assert session_value["artifacts"] == {"implement.session": reference(SESSION_ID)}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\xff", "PA002"),
        (b"\xef\xbb\xbf{}", "PA002"),
        (b"[]", "PA002"),
        (b'{"x":{"y":{"z":{}}}}', "PA002"),
        (b'{"x":"\\ud800"}', "PA003"),
        (b'{"reason":"private-feedback-marker","reason":""}', "PA002"),
        (
            b'{"artifacts":{"a.b":{"artifact_version_id":"","artifact_version_id":""}}}',
            "PA002",
        ),
        (b'{"artifacts":{"a.b":{},"a\\u002eb":{}}}', "PA002"),
        (b'{"subgraph_attempt":1.0}', "PA002"),
        (b'{"subgraph_attempt":1e0}', "PA002"),
        (b'{"subgraph_attempt":NaN}', "PA002"),
        (b'{"subgraph_attempt":100}', "PA002"),
        (b'{"subgraph_attempt":0}', "PA002"),
        (b'{"subgraph_attempt":-1}', "PA002"),
        (
            b'{"reason":"private-feedback-marker","extra":null,"artifacts":{},"error_class":"timeout"}',
            "PA003",
        ),
        (
            b'{"artifacts":{},"error_class":"timeout","reason":"","subgraph_attempt":true}',
            "PA003",
        ),
        (
            b'{"artifacts":{},"error_class":"timeout","reason":"\\ud800","subgraph_attempt":1}',
            "PA003",
        ),
        (
            b' {"artifacts":{},"error_class":"timeout",'
            b'"reason":"","subgraph_attempt":1}',
            "PA006",
        ),
    ],
)
def test_previous_attempt_rejects_invalid_payloads(
    payload: bytes, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(PreviousAttemptValidationError) as rejected:
        parse_previous_attempt_json(payload)

    assert rejected.value.violation.code == code
    assert rejected.value.violation.path == ()
    assert rejected.value.__context__ is None
    assert rejected.value.__cause__ is None
    assert MARKER not in repr(rejected.value)
    assert MARKER not in caplog.text


class TextSubclass(str):
    pass


@pytest.mark.parametrize(
    "value",
    [
        evidence() | {"artifacts": None},
        evidence() | {"reason": TextSubclass(MARKER)},
        evidence() | {"reason": "\ud800"},
        evidence() | {"subgraph_attempt": True},
        evidence() | {"subgraph_attempt": 1.0},
        evidence(ordinal=100),
        evidence(error_class=MARKER),
        evidence({"a.b": None}),
        evidence({"a.b": {"artifact_version_id": GATE_ID, "bytes": MARKER}}),
        evidence({"a.b": {"artifact_version_id": "8" + GATE_ID[1:]}}),
        evidence({"a.b": reference(GATE_ID.lower())}),
        evidence({"a.b": reference(GATE_ID.replace("F", "I"))}),
        evidence({TextSubclass("a.b"): reference(GATE_ID)}),
        evidence({"a/" + MARKER: reference(GATE_ID)}),
        evidence({"a." + "x" * 65: reference(GATE_ID)}),
        evidence() | {MARKER: "private"},
    ],
)
def test_previous_attempt_rejects_out_of_domain_native_values(value: object) -> None:
    with pytest.raises(PreviousAttemptValidationError) as rejected:
        PreviousAttempt(value)

    assert rejected.value.violation.code == "PA003"
    assert rejected.value.violation.path == ()
    assert rejected.value.__context__ is None
    assert MARKER not in repr(rejected.value)


def overbound_artifact_maps() -> bytes:
    entries = b",".join(
        f'"a.a{index}":{{}}'.encode() for index in range(MAX_ARTIFACTS // 2 + 1)
    )
    return b'{"artifacts":{' + entries + b'},"artifacts":{' + entries + b"}}"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"x" * (MAX_JSON_BYTES + 1), "PA001"),
        (b'{"a":null,"b":null,"c":null,"d":null,"e":null}', "PA002"),
        (b'{"a","b","c","d","e"}', "PA002"),
        (b'{"artifacts":{' + b'"a.b":{},' * MAX_ARTIFACTS + b'"a.c":{}}}', "PA002"),
        (overbound_artifact_maps(), "PA002"),
        (b'{"artifacts":{"a.b":{"a":null,"b":null}}}', "PA002"),
        (b'{"artifacts":{"a.b":{"a"}},"reason":[0]}', "PA002"),
    ],
)
def test_previous_attempt_admission_is_bounded_and_content_free(
    payload: bytes, code: str
) -> None:
    calls = ObservedCalls()

    with (
        observing_calls(calls, parse_previous_attempt_json.__code__),
        pytest.raises(PreviousAttemptValidationError) as rejected,
    ):
        parse_previous_attempt_json(payload)

    assert rejected.value.violation.code == code
    assert rejected.value.violation.path == ()
    assert json.loads.__code__ not in calls.functions
    assert encode_snapshot.__code__ not in calls.functions


def test_previous_attempt_native_admission_precedes_copy_and_sort() -> None:
    wide: JsonObject = {
        f"a.a{index}": reference(GATE_ID) for index in range(MAX_ARTIFACTS + 1)
    }
    almost_empty = evidence(error_class="timeout", reason="")
    remaining = MAX_JSON_BYTES - len(encode_snapshot(almost_empty))
    escaped = evidence(error_class="timeout", reason="\0" * (remaining // 6 + 1))
    calls = ObservedCalls()

    with observing_calls(calls, PreviousAttempt.__init__.__code__):
        for value in (evidence(wide), escaped):
            with pytest.raises(PreviousAttemptValidationError) as rejected:
                PreviousAttempt(value)
            assert rejected.value.violation.code == "PA004"
            assert rejected.value.__context__ is None

    assert snapshot.__code__ not in calls.functions
    assert encode_snapshot.__code__ not in calls.functions


def test_previous_attempt_accepts_exact_raw_and_canonical_byte_limits() -> None:
    smallest = evidence(error_class="timeout", reason="")
    remaining = MAX_JSON_BYTES - len(encode_snapshot(smallest))
    edge = evidence(error_class="timeout", reason="x" * remaining)

    canonical = PreviousAttempt(edge).canonical_bytes()
    parsed = parse_previous_attempt_json(canonical)

    assert len(canonical) == MAX_JSON_BYTES
    assert parsed.value == edge


def test_previous_attempt_requires_expected_digest_and_canonical_bytes() -> None:
    canonical = PreviousAttempt(evidence()).canonical_bytes()
    formatted = b" " + canonical

    with pytest.raises(PreviousAttemptValidationError) as mismatch:
        parse_retained_previous_attempt(
            canonical, expected_payload_digest="sha256:" + "0" * 64
        )
    with pytest.raises(PreviousAttemptValidationError) as noncanonical:
        parse_retained_previous_attempt(
            formatted, expected_payload_digest="sha256:" + sha256(formatted).hexdigest()
        )

    assert mismatch.value.violation.code == noncanonical.value.violation.code == "PA006"
    assert MARKER not in repr(mismatch.value)
    assert MARKER not in repr(noncanonical.value)


def test_previous_attempt_refusals_release_codec_traceback_data() -> None:
    canonical = PreviousAttempt(evidence({"a.b": reference(GATE_ID)})).canonical_bytes()
    wrong_digest = "sha256:" + "d" * 64
    native = evidence({"a." + MARKER: reference(GATE_ID)})

    with pytest.raises(PreviousAttemptValidationError) as bad_native:
        PreviousAttempt(native)
    with pytest.raises(PreviousAttemptValidationError) as bad_wire:
        parse_previous_attempt_json(b" " + canonical)
    try:
        raise ValueError(MARKER)
    except ValueError:
        with pytest.raises(PreviousAttemptValidationError) as bad_digest:
            parse_retained_previous_attempt(
                canonical, expected_payload_digest=wrong_digest
            )
    with pytest.raises(PreviousAttemptValidationError) as bad_descriptor:
        require_previous_attempt_revision({MARKER: GATE_ID})

    for refusal in (bad_native, bad_wire, bad_digest, bad_descriptor):
        error = refusal.value
        codec_locals = [
            repr(frame.f_locals)
            for frame, _ in walk_tb(error.__traceback__)
            if frame.f_globals.get("__name__") == PreviousAttempt.__module__
        ]
        assert error.__cause__ is error.__context__ is None
        assert MARKER not in "".join(format_exception(error))
        assert codec_locals
        assert all(
            marker not in local_values
            for marker in (MARKER, GATE_ID, wrong_digest)
            for local_values in codec_locals
        )


def test_previous_attempt_revision_pins_schema() -> None:
    schema_path = ARTIFACTS / "PreviousAttempt.schema.json"
    descriptor_path = ARTIFACTS / "PreviousAttempt.revision-1.json"
    schema = cast(JsonObject, read_json(schema_path))
    descriptor = previous_attempt_revision_descriptor()
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )
    properties = cast(JsonObject, schema["properties"])
    classes = cast(JsonObject, properties["error_class"])
    fields: JsonObject = reference(GATE_ID)
    supplied = evidence({"a.b": fields})

    cast("SchemaClass", Draft202012Validator).check_schema(schema)
    require_previous_attempt_revision(descriptor)
    model = PreviousAttempt(supplied)
    fields["artifact_version_id"] = VCS_ID
    model.value["reason"] = ""
    descriptor["schema_sha256"] = MARKER
    with pytest.raises(PreviousAttemptValidationError) as rejected:
        require_previous_attempt_revision(descriptor)

    assert sha256(schema_path.read_bytes()).hexdigest() == SCHEMA_HASH
    assert sha256(descriptor_path.read_bytes()).hexdigest() == DESCRIPTOR_HASH
    assert previous_attempt_revision_descriptor() == read_json(descriptor_path)
    assert classes["enum"] == [item.value for item in ErrorClass]
    assert all(validator.is_valid(row["value"]) for row in vectors())
    assert model.value == evidence({"a.b": reference(GATE_ID)})
    assert repr(model) == "PreviousAttempt()"
    assert rejected.value.violation.code == "PA005"
    assert MARKER not in repr(rejected.value)


def test_previous_attempt_does_not_admit_live_retry_or_declaration() -> None:
    document = GraphDocument.model_validate_json(EXAMPLE.read_bytes())

    declarations = validate_type_declarations(document)
    configuration = validate_first_slice_configuration(document)

    assert not declarations.declarations_valid
    assert [
        (error.code, (error.path or ())[-2:])
        for error in declarations.diagnostics.errors
    ] == [("IR022", ("previous_attempt", "type"))]
    assert not configuration.authored_configuration_supported
    configuration_errors = configuration.diagnostics.errors
    assert ("IR028", ("retry", "max_attempts")) in {
        (error.code, (error.path or ())[-2:]) for error in configuration_errors
    }
