import pytest
from pydantic import MISSING, ValidationError
from pydantic_core import PydanticCustomError

from kora.ir import (
    Concurrency,
    ProviderRetry,
    RuntimeSection,
    SecretsSection,
    TargetConfig,
    export_ir,
    render_ir_json,
)
from kora.ir.json import JsonValue
from tests.ir.declaration_cases import MINIMAL, POPULATED


def test_ir_named_maps_preserve_entry_contracts() -> None:
    runtime = {
        "runtime": "",
        "vcs": "",
        "approval_channels": {"x-channel": {"x-data": False}, "": {"enabled": True}},
    }
    aliases = {"aliases": {"TOKEN": "secret://env/key", "OTHER": "secret://vault/path"}}

    parsed = RuntimeSection.model_validate(runtime)
    secrets = SecretsSection.model_validate(aliases)
    with pytest.raises(ValidationError):
        SecretsSection.model_validate({"aliases": {"x-token": "secret://env/key"}})
    with pytest.raises(ValidationError):
        RuntimeSection.model_validate(
            {**runtime, "approval_channels": {"x-channel": False}}
        )

    assert export_ir(parsed) == runtime
    assert export_ir(secrets) == aliases
    assert parsed.approval_channels is not MISSING
    assert list(parsed.approval_channels) == ["x-channel", ""]


def test_ir_approval_channel_data_and_exports_are_detached() -> None:
    from typing import cast

    from kora.ir.json import JsonObject

    shared: dict[str, object] = {"values": [None, False]}
    document = {
        "runtime": "",
        "vcs": "",
        "approval_channels": {"first": shared, "second": shared},
    }

    parsed = RuntimeSection.model_validate(document)
    shared["raw"] = True
    first = export_ir(parsed)
    channels = cast("JsonObject", first["approval_channels"])
    cast("JsonObject", channels["first"])["exported"] = True
    second = export_ir(parsed)

    assert second == {
        "runtime": "",
        "vcs": "",
        "approval_channels": {
            "first": {"values": [None, False]},
            "second": {"values": [None, False]},
        },
    }


def test_ir_approval_channel_exports_refuse_mutated_shared_data() -> None:
    parsed = RuntimeSection.model_validate(
        {"runtime": "", "vcs": "", "approval_channels": {"channel": {}}}
    )
    assert parsed.approval_channels is not MISSING
    data = parsed.approval_channels["channel"]
    shared: list[JsonValue] = []
    data["first"] = shared
    data["second"] = shared

    with pytest.raises(PydanticCustomError) as export_refusal:
        export_ir(parsed)
    with pytest.raises(PydanticCustomError) as render_refusal:
        render_ir_json(parsed)

    assert export_refusal.value.type == render_refusal.value.type == "ir_data_invalid"


@pytest.mark.parametrize("replacement", ["repeated", "array", "shared_fields"])
def test_ir_approval_channel_exports_validate_replaced_values(replacement: str) -> None:
    from typing import cast

    parsed = RuntimeSection.model_validate(
        {"runtime": "", "vcs": "", "approval_channels": {"first": {}, "second": {}}}
    )
    channels = cast("dict[str, object]", parsed.approval_channels)
    shared: list[JsonValue] = []
    if replacement == "repeated":
        channels["first"] = {"a": shared, "b": shared}
    elif replacement == "array":
        channels["first"] = []
    else:
        channels["first"] = {"a": shared}
        channels["second"] = channels["first"]

    if replacement == "shared_fields":
        exported = export_ir(parsed)
        assert exported == {
            "runtime": "",
            "vcs": "",
            "approval_channels": {"first": {"a": []}, "second": {"a": []}},
        }
    else:
        with pytest.raises(PydanticCustomError) as export_refusal:
            export_ir(parsed)
        with pytest.raises(PydanticCustomError) as render_refusal:
            render_ir_json(parsed)
        assert export_refusal.value.type == "ir_data_invalid"
        assert render_refusal.value.type == "ir_data_invalid"


def test_ir_runtime_preserves_disabled_policy_omissions() -> None:
    documents: list[dict[str, object]] = [
        {"runtime": "", "vcs": ""},
        {"runtime": "", "vcs": "", "provider_retry": {}},
        {
            "runtime": "",
            "vcs": "",
            "provider_retry": {"max_wait": "0000s", "x-meta": False},
        },
    ]

    parsed = [RuntimeSection.model_validate(document) for document in documents]
    policy = ProviderRetry.model_validate({})

    assert [export_ir(item) for item in parsed] == documents
    assert parsed[0].provider_retry is MISSING
    assert policy.max_wait is MISSING


@pytest.mark.parametrize(
    "value", [False, None, -1, 100.1, float("inf"), float("nan"), "0"]
)
def test_ir_runtime_budget_headroom_refuses_nonfinite_and_out_of_range(
    value: object,
) -> None:
    document = {**MINIMAL[RuntimeSection], "budget_headroom_pct": value}

    with pytest.raises(ValidationError):
        RuntimeSection.model_validate(document)


@pytest.mark.parametrize("value", [0, 1, 1.5, 100, 100.0])
def test_ir_runtime_budget_headroom_preserves_json_number(value: int | float) -> None:
    document = {**MINIMAL[RuntimeSection], "budget_headroom_pct": value}

    parsed = RuntimeSection.model_validate(document)

    assert parsed.budget_headroom_pct == value
    assert type(parsed.budget_headroom_pct) is type(value)


def test_ir_declaration_optional_fields_refuse_null_and_unknown_fields() -> None:
    cases = [
        (model, {**MINIMAL[model], name: None})
        for model, document in POPULATED.items()
        for name in document
        if name != "parent" and not name.startswith("x-")
    ]

    for model, document in cases:
        with pytest.raises(ValidationError):
            model.model_validate(document)
    for model, document in MINIMAL.items():
        with pytest.raises(ValidationError):
            model.model_validate({**document, "unknown": False})


@pytest.mark.parametrize("value", [False, 0, 1.5, "1", None])
def test_ir_concurrency_refuses_nonpositive_or_noninteger_values(value: object) -> None:
    document = {"max_running_nodes": value}

    with pytest.raises(ValidationError):
        Concurrency.model_validate(document)


@pytest.mark.parametrize(
    "key",
    [
        {},
        {"param": "a", "from": "node", "output": "out", "path": "x"},
        {"from": "node", "output": "", "path": "x"},
        {"from": "node", "output": "out", "path": "x" * 4097},
        {"from_": "node", "output": "out", "path": "x"},
    ],
)
def test_ir_target_key_variants_are_exclusive_and_aliases_are_strict(
    key: object,
) -> None:
    document = {"key": key}

    with pytest.raises(ValidationError):
        TargetConfig.model_validate(document)
