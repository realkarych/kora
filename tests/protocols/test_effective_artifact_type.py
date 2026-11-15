from dataclasses import FrozenInstanceError, fields
from typing import cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir.json import JsonValue
from kora.ir.structured_json import MAX_JSON_BYTES
from kora.ir.structured_output import parse_structured_output
from kora.protocols.effective_artifact_type import (
    EffectiveArtifactType,
    EffectiveInvalid,
    EffectiveRefusal,
    EffectiveValid,
    SummaryBinding,
    ValidationBinding,
    prepare_effective_artifact_type,
)
from tests.conformance.artifact_types import payload
from tests.ir.standard_output_cases import output_input
from tests.ir.structured_schema_cases import builtin, contract, source
from tests.plugins.artifact_type.structured_output_support import ROOT_FACTORIES
from tests.plugins.artifact_type.test_structured_output import HostileValue
from tests.protocols.effective_artifact_cases import (
    ObservedArtifactType,
    install_custom,
    installed,
    prepared,
)


@pytest.mark.parametrize(
    ("schema", "value", "outcome"),
    [
        ({"multipleOf": 0.1}, 0.3, "valid"),
        ({"type": "integer"}, 1.0, "valid"),
        ({"const": 9007199254740993}, 9007199254740993, "valid"),
        ({"const": 0}, -0.0, "valid"),
        ({"const": [[True]]}, [[1]], "invalid"),
        ({"uniqueItems": True}, [[1], [True], [1]], "invalid"),
        ({"uniqueItems": True}, [[1], [True]], "valid"),
        ({"allOf": [{"minimum": 1}, {"maximum": 3}]}, 2, "valid"),
        ({"anyOf": [False, {"type": "number"}]}, 2, "valid"),
        ({"oneOf": [True, True]}, 2, "invalid"),
        ({"not": False}, None, "valid"),
        ({"if": {"type": "number"}, "then": False, "else": True}, "x", "valid"),
    ],
)
def test_effective_schema_generic_behavior_uses_exact_numbers(
    schema: object, value: JsonValue, outcome: str, effective_capture: Capture
) -> None:
    selected = prepared(output_input(schema))
    original = parse_structured_output(value).canonical_bytes()

    result = selected.validate(value, context_for(effective_capture))

    assert result.outcome == outcome
    if isinstance(result, EffectiveValid):
        assert result.value.canonical_bytes() == original
        assert result.digest_fields(context_for(effective_capture)) == [""]
    else:
        assert isinstance(result, EffectiveInvalid)
        assert result.violation is not None
        assert result.violation.code == "SO006"
        assert result.violation.path == ()


@pytest.mark.parametrize("rejected_role", ["parent", "declared", "inline", "none"])
def test_effective_schema_composition_preserves_every_contract(
    rejected_role: str, effective_capture: Capture
) -> None:
    parent = contract(rejected_role != "parent", "example/Parent")
    selected = contract(rejected_role != "declared", parent="example/Parent")
    for record in (parent, selected):
        record["behavior"] = cast(
            "list[dict[str, object]]", output_input().type_contracts
        )[1]["behavior"]
    inline = {
        "source": source("inline", "inline_schema"),
        "schema": rejected_role != "inline",
    }
    operation = prepared(
        output_input(records=[selected, builtin(), parent], inline=inline)
    )

    result = operation.validate(None, context_for(effective_capture))

    assert result.outcome == ("valid" if rejected_role == "none" else "invalid")
    if rejected_role != "none":
        assert not hasattr(result, "summary")
        assert not hasattr(result, "digest_fields")


def test_reference_only_schema_is_not_an_extra_assertion(
    effective_capture: Capture,
) -> None:
    reference = contract(
        {"type": "string", "$defs": {"number": {"type": "number"}}}, "example/Reference"
    )
    candidate = output_input(
        {"$ref": "urn:kora:artifact:example:Reference#/$defs/number"}
    )
    cast("list[dict[str, object]]", candidate.type_contracts).append(reference)
    operation = prepared(candidate)

    result = operation.validate(0.3, context_for(effective_capture))

    assert isinstance(result, EffectiveValid)


@pytest.mark.parametrize("factory", ROOT_FACTORIES)
def test_effective_identity_preserves_every_root(
    factory: object, effective_capture: Capture
) -> None:
    assert callable(factory)
    value: object = factory()
    operation = prepared()
    original = parse_structured_output(value).canonical_bytes()
    ctx = context_for(effective_capture)

    result = operation.validate(value, ctx)
    wire_result = operation.validate_json(original, ctx)

    assert isinstance(result, EffectiveValid)
    assert isinstance(wire_result, EffectiveValid)
    assert result.value.canonical_bytes() == original
    assert wire_result.value.canonical_bytes() == original
    assert parse_structured_output(result.summary(ctx)).canonical_bytes() == original


@pytest.mark.parametrize(
    ("value", "code"),
    [
        pytest.param(HostileValue(), "SO003", id="hostile"),
        pytest.param(float("inf"), "SO003", id="infinity"),
        pytest.param(1 << 14000, "SO003", id="integer"),
        pytest.param("x" * MAX_JSON_BYTES, "SO004", id="canonical_size"),
    ],
)
def test_structured_output_effective_validation_cannot_waive_parent_admission(
    value: object, code: str, effective_capture: Capture
) -> None:
    operation = prepared(output_input(True))

    result = operation.validate(value, context_for(effective_capture))

    assert isinstance(result, EffectiveInvalid)
    assert result.violation is not None
    assert result.violation.code == code
    assert not hasattr(result, "value")


@pytest.mark.parametrize(
    ("wire", "code"),
    [
        pytest.param(b" " * (MAX_JSON_BYTES + 1), "SO001", id="raw_size"),
        (b'{"x":1,"x":2}', "SO002"),
        (b"NaN", "SO002"),
    ],
)
def test_effective_raw_ingress_preserves_original_refusals(
    wire: bytes, code: str, effective_capture: Capture
) -> None:
    operation = prepared()

    result = operation.validate_json(wire, context_for(effective_capture))

    assert isinstance(result, EffectiveInvalid)
    assert result.violation is not None
    assert result.violation.code == code


def test_effective_additional_shape_refusal_is_not_false(
    effective_capture: Capture,
) -> None:
    value = [0] * 65537
    admitted = parse_structured_output(value)
    operation = prepared()

    result = operation.validate(admitted, context_for(effective_capture))

    assert isinstance(result, EffectiveRefusal)
    assert result.reason == "evaluation_exhausted"
    assert not hasattr(result, "violation")
    assert not hasattr(result, "summary")


@pytest.mark.parametrize("defect", ["missing", "identity", "role", "stock_generic"])
def test_unknown_or_incompatible_bindings_refuse_before_value_work(defect: str) -> None:
    candidate = output_input()
    bindings = installed()
    key = ("validation", "kora.json-schema-2020-12.v1")
    provider = ObservedArtifactType()
    if defect == "missing":
        del bindings["validation", key[1]]
    elif defect == "identity":
        bindings["validation", key[1]] = ValidationBinding(
            "different", provider.validate
        )
    elif defect == "role":
        bindings["validation", key[1]] = SummaryBinding(key[1], provider.summary)
    else:
        bindings["validation", key[1]] = ValidationBinding(key[1], provider.validate)

    result = prepare_effective_artifact_type(candidate, bindings)

    assert isinstance(result, EffectiveRefusal)
    assert result.reason == "unsupported_schema"
    assert provider.calls == []


def test_structured_output_effective_binding_is_immutable(
    effective_capture: Capture,
) -> None:
    candidate = output_input({"type": "number"})
    bindings = installed()
    result = prepare_effective_artifact_type(candidate, bindings)
    assert isinstance(result, EffectiveArtifactType)
    original_bytes = result.source_bytes
    selected = cast("list[dict[str, object]]", candidate.type_contracts)[1]
    selected["schema"] = False
    cast("dict[str, object]", candidate.schema_validation)["policy"] = "different"
    bindings.clear()

    accepted = result.validate(0.3, context_for(effective_capture))
    rejected = result.validate("x", context_for(effective_capture))

    assert isinstance(accepted, EffectiveValid)
    assert isinstance(rejected, EffectiveInvalid)
    assert result.source_bytes == original_bytes
    attribute = fields(result)[0].name
    with pytest.raises(FrozenInstanceError):
        setattr(cast("object", result), attribute, b"{}")


@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "mutate_validation_input",
        "mutate_selection_input",
        "mutate_summary_input",
    ],
)
def test_effective_schema_extraction_requires_complete_validity(
    defect: str, effective_capture: Capture
) -> None:
    provider = ObservedArtifactType(defect=defect)
    candidate = output_input(
        records=[builtin(), contract({"properties": {"left": {"const": "first"}}})]
    )
    bindings = installed()
    install_custom(bindings, provider)
    operation = prepare_effective_artifact_type(candidate, bindings)
    assert isinstance(operation, EffectiveArtifactType)
    ctx = context_for(effective_capture)
    value = payload()
    original = parse_structured_output(value).canonical_bytes()

    result = operation.validate(value, ctx)
    assert isinstance(result, EffectiveValid)
    first_selection = result.digest_fields(ctx)
    first_summary = result.summary(ctx)
    assert isinstance(first_summary, dict)
    first_selection.append("/changed")
    first_summary["nested"] = ["changed"]
    second_selection = result.digest_fields(ctx)
    second_summary = result.summary(ctx)

    assert second_selection == ["/right"]
    assert second_summary == {"nested": ["second"]}
    assert result.value.canonical_bytes() == original
    assert provider.left_values == ["first"] * len(provider.left_values)


@pytest.mark.parametrize("failure", ["custom", "schema", "capacity"])
def test_invalid_or_refused_has_no_extraction_hooks(
    failure: str, effective_capture: Capture
) -> None:
    provider = ObservedArtifactType()
    candidate = output_input(
        records=[builtin(), contract(False if failure == "schema" else True)]
    )
    bindings = installed()
    install_custom(bindings, provider)
    operation = prepare_effective_artifact_type(candidate, bindings)
    assert isinstance(operation, EffectiveArtifactType)
    value = {} if failure == "custom" else payload()
    if failure == "capacity":
        assert isinstance(value, dict)
        value["left"] = cast("list[JsonValue]", [0] * 65537)

    result = operation.validate(value, context_for(effective_capture))

    assert not isinstance(result, EffectiveValid)
    assert provider.calls == ["validate"]
    assert not hasattr(result, "digest_fields")
    assert not hasattr(result, "summary")


def test_mutating_parent_does_not_change_later_behavior_or_false_schema(
    effective_capture: Capture,
) -> None:
    parent = contract(True, "example/Parent")
    child = contract(False, parent="example/Parent")
    child["behavior"] = {
        "validation": "child.validation.v1",
        "digest_fields": "child.digest.v1",
        "summary": "child.summary.v1",
    }
    first, second = (
        ObservedArtifactType(defect="mutate_validation_input"),
        ObservedArtifactType(),
    )
    bindings = installed()
    install_custom(bindings, first)
    install_custom(bindings, second, "child")
    candidate = output_input(records=[builtin(), parent, child])
    operation = prepare_effective_artifact_type(candidate, bindings)
    assert isinstance(operation, EffectiveArtifactType)

    result = operation.validate(payload(), context_for(effective_capture))

    assert isinstance(result, EffectiveInvalid)
    assert result.reason == "schema_mismatch"
    assert first.left_values == ["first"]
    assert second.left_values == ["first"]
    assert first.calls == second.calls == ["validate"]


def test_descriptor_violation_is_fixed_and_content_free() -> None:
    root = builtin()
    cast("dict[str, object]", root["descriptor"])["private"] = "private marker"
    candidate = output_input(records=[root], declared="StructuredOutput")

    result = prepare_effective_artifact_type(candidate, installed())

    assert isinstance(result, EffectiveInvalid)
    assert result.violation is not None
    assert result.violation.code == "SO005"
    assert "private" not in repr(result)
