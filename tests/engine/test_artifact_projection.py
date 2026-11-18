import hashlib
import json
import math
from dataclasses import replace
from typing import cast

import pytest

from kora.cel_eval.artifact_projection_codec import (
    ProjectionFailure,
    ProjectionRejected,
)
from kora.conformance.common import context_for
from kora.engine.artifact_projection import (
    ProjectionReadMetadata,
    check_artifact_projection,
    prepare_artifact_projection,
)
from kora.engine.artifact_projection_source import projection_source
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_output import StructuredOutput
from kora.protocols.effective_artifact_type import (
    EffectiveArtifactType,
    EffectiveValid,
    prepare_effective_artifact_type,
)
from tests.engine.artifact_projection_cases import (
    LIMITS,
    PATH,
    metadata,
    native_values,
    projection_capture,
    projection_graph,
)
from tests.engine.graph_output_cases import child, output_document
from tests.engine.graph_schema_cases import declaration
from tests.protocols.effective_artifact_cases import installed


@pytest.mark.parametrize(
    ("schema", "value", "converted"),
    [
        ({"type": "integer"}, 1.0, 1),
        ({"type": "integer"}, -0.0, 0),
        ({"type": "integer"}, 9007199254740993, 9007199254740993),
        ({"type": "integer"}, 9223372036854775807, 9223372036854775807),
        ({"type": "integer"}, -9223372036854775808, -9223372036854775808),
        ({"type": "number"}, 9007199254740993, 9007199254740992.0),
        ({"type": "number"}, 0.0, 0.0),
        ({"type": "number"}, 1.7976931348623157e308, 1.7976931348623157e308),
        ({}, None, None),
        ({}, True, True),
        ({}, "nul\0and😀", "nul\0and😀"),
        ({}, [], []),
        ({}, {}, {}),
        ({}, [1, 1.0, -0.0], [1.0, 1.0, -0.0]),
        ({}, {"\0😀": -0.0}, {"\0😀": -0.0}),
        ({"type": "number"}, 5e-324, 5e-324),
    ],
)
def test_projection_keeps_original_and_saved_conversion(
    schema: JsonObject, value: JsonValue, converted: JsonValue
) -> None:
    checked = projection_graph(schema)
    original = StructuredOutput(value).canonical_bytes()
    expected_converted = StructuredOutput(converted).canonical_bytes()

    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, value, installed(), context_for(capture), limits=LIMITS
        )
        restored = check_artifact_projection(
            checked, PATH, prepared.encoded(), metadata(prepared), limits=LIMITS
        )

    before, after = native_values(json.loads(restored.encoded()))
    assert StructuredOutput(before).canonical_bytes() == original
    assert StructuredOutput(after).canonical_bytes() == expected_converted
    assert prepared.encoded() == restored.encoded()
    assert restored.identity.payload_digest == hashlib.sha256(original).hexdigest()
    assert restored.content_digest == hashlib.sha256(restored.encoded()).hexdigest()
    if type(value) is float and value == 0:
        assert type(before) is float
        assert math.copysign(1.0, before) == math.copysign(1.0, value)


def test_projection_requires_effective_validation_and_installed_behavior() -> None:
    checked = projection_graph({"type": "integer", "minimum": 2})
    standalone = StructuredOutput(1)

    with projection_capture() as capture:
        with pytest.raises(ProjectionRejected) as invalid:
            prepare_artifact_projection(
                checked,
                PATH,
                standalone,
                installed(),
                context_for(capture),
                limits=LIMITS,
            )
        with pytest.raises(ProjectionRejected) as unavailable:
            prepare_artifact_projection(
                checked, PATH, 2, {}, context_for(capture), limits=LIMITS
            )

    assert invalid.value.reason is ProjectionFailure.VALIDATION
    assert unavailable.value.reason is ProjectionFailure.UNAVAILABLE
    assert standalone.canonical_bytes() == b"1"


def test_projection_keeps_schema_capacity_refusal_distinct_from_invalid_value() -> None:
    checked = projection_graph({"type": "array", "uniqueItems": True})

    with projection_capture() as capture, pytest.raises(ProjectionRejected) as refused:
        prepare_artifact_projection(
            checked,
            PATH,
            list(range(129)),
            installed(),
            context_for(capture),
            limits=LIMITS,
        )

    assert refused.value.reason is ProjectionFailure.CAPACITY


def test_projection_does_not_execute_inert_schema_dependencies() -> None:
    document = output_document()
    child(document, "type_contracts")["example/Inert"] = declaration(
        {"pattern": "unsupported-but-inert"}
    )
    checked = projection_graph(
        {"type": "integer", "$recursiveRef": "urn:kora:artifact:example:Inert"},
        document=document,
    )

    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, 7, installed(), context_for(capture), limits=LIMITS
        )
        restored = check_artifact_projection(
            checked, PATH, prepared.encoded(), metadata(prepared), limits=LIMITS
        )

    assert native_values(json.loads(restored.encoded())) == (7, 7)


def test_projection_does_not_accept_unassociated_validation_as_payload() -> None:
    checked = projection_graph({"type": "integer"})
    source = projection_source(checked, PATH, LIMITS, validating=True)
    effective = prepare_effective_artifact_type(source.schema, installed())
    assert isinstance(effective, EffectiveArtifactType)
    with projection_capture() as capture:
        valid = effective.validate(7, context_for(capture))
    assert isinstance(valid, EffectiveValid)

    with projection_capture() as capture, pytest.raises(ProjectionRejected) as refused:
        prepare_artifact_projection(
            checked, PATH, valid, installed(), context_for(capture), limits=LIMITS
        )

    assert refused.value.reason is ProjectionFailure.VALIDATION


@pytest.mark.parametrize(
    ("name", "reason"),
    [("session", ProjectionFailure.UNAVAILABLE), ("absent", ProjectionFailure.BINDING)],
)
def test_projection_refuses_other_artifact_kinds_and_unknown_outputs(
    name: str, reason: ProjectionFailure
) -> None:
    checked = projection_graph()
    path = (*PATH[:-1], name)

    with projection_capture() as capture, pytest.raises(ProjectionRejected) as refused:
        prepare_artifact_projection(
            checked, path, None, installed(), context_for(capture), limits=LIMITS
        )

    assert refused.value.reason is reason


def test_projection_handoff_cannot_swap_equal_output_values() -> None:
    checked = projection_graph({"type": "integer"}, second=True)
    other = ("graph", "nodes", "another", "outputs", "value")

    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, 7, installed(), context_for(capture), limits=LIMITS
        )
        separately_prepared = prepare_artifact_projection(
            checked, other, 7, installed(), context_for(capture), limits=LIMITS
        )
        with pytest.raises(ProjectionRejected) as swapped:
            check_artifact_projection(
                checked, other, prepared.encoded(), metadata(prepared), limits=LIMITS
            )

    assert (
        prepared.identity.payload_digest == separately_prepared.identity.payload_digest
    )
    assert prepared.binding != separately_prepared.binding
    assert swapped.value.reason is ProjectionFailure.BINDING


@pytest.mark.parametrize(
    "member", ["contract_digest", "payload_digest", "path_set_digest"]
)
def test_projection_rejects_expected_identity_mismatch(member: str) -> None:
    checked = projection_graph()
    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked,
            PATH,
            {"number": -0.0},
            installed(),
            context_for(capture),
            limits=LIMITS,
        )
    owner = metadata(prepared)
    wrong = replace(owner, identity=replace(owner.identity, **{member: "f" * 64}))

    with pytest.raises(ProjectionRejected) as refused:
        check_artifact_projection(
            checked, PATH, prepared.encoded(), wrong, limits=LIMITS
        )

    assert refused.value.reason is ProjectionFailure.BINDING
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None


def test_projection_keeps_large_unselected_model_domain() -> None:
    checked = projection_graph(
        {"type": "object", "properties": {"flag": {"type": "boolean"}}},
        "has(inputs.data) && has(inputs.data.flag) && inputs.data.flag",
    )
    value: JsonValue = {"flag": True, "unselected": [10**1000, "x" * 50_000]}

    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, value, installed(), context_for(capture), limits=LIMITS
        )

    before, after = native_values(json.loads(prepared.encoded()))
    assert before == after == {"flag": True}
    assert (
        prepared.identity.payload_digest
        == hashlib.sha256(StructuredOutput(value).canonical_bytes()).hexdigest()
    )
    assert len(prepared.encoded()) < 4096


@pytest.mark.parametrize("state", ["expired", "unknown", "legacy"])
def test_projection_requires_explicit_current_owner_metadata(state: str) -> None:
    checked = projection_graph()
    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, None, installed(), context_for(capture), limits=LIMITS
        )
    owner = metadata(prepared)
    if state == "expired":
        owner = replace(owner, available=False)
    elif state == "unknown":
        owner = replace(owner, representation="unrecognized")
    else:
        owner = cast(ProjectionReadMetadata, None)

    with pytest.raises(ProjectionRejected) as refused:
        check_artifact_projection(
            checked, PATH, prepared.encoded(), owner, limits=LIMITS
        )

    assert refused.value.reason is ProjectionFailure.UNAVAILABLE
