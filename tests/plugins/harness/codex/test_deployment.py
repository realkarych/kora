from typing import Final, Protocol, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from kora.ir.json import JsonObject, JsonValue
from kora.plugins.harness.codex.adapter import CodexHarness
from kora.plugins.harness.codex.deployment import (
    CONFIG_SCHEMA,
    CodexDeployment,
)
from kora.plugins.harness.quota import MAX_RELEASE_BATCH, MAX_RELEASE_INTERVAL


class SchemaValidator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


CREDENTIALS: Final[JsonObject] = {"CODEX_API_KEY": "secret://env/CODEX_API_KEY"}
SHAPE_CASES: Final[tuple[tuple[JsonObject, bool], ...]] = (
    ({"credentials": CREDENTIALS}, True),
    ({}, False),
    ({"credentials": {}}, False),
    ({"credentials": None}, False),
    ({"credentials": {"CODEX_API_KEY": "plain-private-key"}}, False),
    ({"credentials": {"CODEX_API_KEY": "secret://env/KEY\n"}}, False),
    ({"credentials": {"CODEX_API_KEY": 12}}, False),
    ({"credentials": {**CREDENTIALS, "extra": "private"}}, False),
    ({"credentials": CREDENTIALS, "extra": True}, False),
    ({"credentials": {"ANTHROPIC_API_KEY": "secret://env/ANTHROPIC_API_KEY"}}, False),
)
QUOTA_CASES: Final[tuple[tuple[JsonValue, bool], ...]] = (
    ({}, True),
    ({"resource": "account-1"}, True),
    ({"resource": ""}, False),
    ({"resource": "account\n"}, False),
    ({"resource": None}, False),
    ({"resource": 1}, False),
    ({"resource": "a" * 257}, False),
    ({"extra": 1}, False),
    (None, False),
    ([], False),
    ({"groups": {"custom": ["*"]}, "vendor_groups": {"window": "custom"}}, True),
    ({"groups": {}, "vendor_groups": {}}, True),
    ({"groups": {"custom": []}, "vendor_groups": {}}, False),
    ({"groups": {"custom": [""]}, "vendor_groups": {}}, False),
    ({"groups": {"custom": [1]}, "vendor_groups": {}}, False),
    ({"groups": {"custom": "*"}, "vendor_groups": {}}, False),
    ({"groups": {"": ["*"]}, "vendor_groups": {}}, False),
    ({"groups": {"custom\n": ["*"]}, "vendor_groups": {}}, False),
    ({"groups": [], "vendor_groups": {}}, False),
    ({"groups": None}, False),
    ({"vendor_groups": {"": "account"}}, False),
    ({"vendor_groups": {"window\n": "account"}}, False),
    ({"vendor_groups": {"window": ""}}, False),
    ({"vendor_groups": {"window": 1}}, False),
    ({"vendor_groups": []}, False),
    ({"vendor_groups": None}, False),
    ({"release_batch": 1}, True),
    ({"release_batch": MAX_RELEASE_BATCH}, True),
    ({"release_batch": 2.0}, True),
    ({"release_batch": 0}, False),
    ({"release_batch": MAX_RELEASE_BATCH + 1}, False),
    ({"release_batch": 1.5}, False),
    ({"release_batch": True}, False),
    ({"release_batch": "1"}, False),
    ({"release_interval": 0.5}, True),
    ({"release_interval": MAX_RELEASE_INTERVAL}, True),
    ({"release_interval": 0}, False),
    ({"release_interval": -1}, False),
    ({"release_interval": MAX_RELEASE_INTERVAL + 1}, False),
    ({"release_interval": True}, False),
    ({"release_interval": "1"}, False),
    ({"vendor_groups": {"window": "missing"}}, False),
)


@pytest.mark.parametrize(("document", "accepted"), SHAPE_CASES)
def test_codex_deployment_refuses_unknown_and_plain_credentials(
    document: JsonObject, accepted: bool
) -> None:
    candidate = dict(document)

    if accepted:
        parsed = CodexDeployment.model_validate(candidate)

        assert parsed.credentials.CODEX_API_KEY == CREDENTIALS["CODEX_API_KEY"]
    else:
        with pytest.raises(ValidationError):
            CodexDeployment.model_validate(candidate)


@pytest.mark.parametrize(("quota", "accepted"), QUOTA_CASES)
def test_codex_deployment_quota_defaults_and_refusals(
    quota: JsonValue, accepted: bool
) -> None:
    document: JsonObject = {"credentials": CREDENTIALS, "quota": quota}
    default_document: JsonObject = {"credentials": CREDENTIALS}

    defaults = CodexDeployment.model_validate(default_document).quota
    try:
        parsed = CodexDeployment.model_validate(document)
    except ValidationError:
        parsed = None

    assert defaults.groups == {"account": ("*",)}
    assert defaults.vendor_groups == {}
    assert defaults.release_batch == 1
    assert defaults.release_interval == 1.0
    assert (parsed is not None) == accepted


def test_codex_config_schema_agrees_with_model() -> None:
    schema = CodexHarness.config_schema
    cases = (
        *SHAPE_CASES,
        *(
            ({"credentials": CREDENTIALS, "quota": quota}, accepted)
            for quota, accepted in QUOTA_CASES
            if quota != {"vendor_groups": {"window": "missing"}}
        ),
    )
    missing_targets: tuple[JsonObject, ...] = (
        {"credentials": CREDENTIALS, "quota": {"vendor_groups": {"window": "missing"}}},
    )
    Draft202012Validator.check_schema(schema)
    validator = cast("SchemaValidator", Draft202012Validator(schema))

    for document, accepted in cases:
        schema_accepts = validator.is_valid(document)
        try:
            CodexDeployment.model_validate(document)
        except ValidationError:
            model_accepts = False
        else:
            model_accepts = True

        assert schema_accepts == model_accepts == accepted, document
    for document in missing_targets:
        schema_accepts = validator.is_valid(document)
        with pytest.raises(ValidationError) as rejected:
            CodexDeployment.model_validate(document)

        assert schema_accepts
        assert [error["type"] for error in rejected.value.errors()] == [
            "quota_group_unknown"
        ]
    assert schema == CONFIG_SCHEMA
