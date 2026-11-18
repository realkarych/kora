from pathlib import Path
from typing import Final, Protocol, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from kora.ir.json import JsonObject, JsonValue
from kora.plugins.harness.claude.adapter import ClaudeHarness
from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.harness.claude.deployment import (
    CONFIG_SCHEMA,
    MAX_RELEASE_BATCH,
    MAX_RELEASE_INTERVAL,
    MAX_RETRIES,
    ClaudeDeployment,
)
from tests.support.claude_launch_probe import placements


class SchemaValidator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


CREDENTIALS: Final[JsonObject] = {"ANTHROPIC_API_KEY": "secret://env/ANTHROPIC_API_KEY"}
SHAPE_CASES: Final[tuple[tuple[JsonObject, bool], ...]] = (
    ({"credentials": CREDENTIALS}, True),
    ({}, False),
    ({"credentials": {}}, False),
    ({"credentials": None}, False),
    ({"credentials": {"ANTHROPIC_API_KEY": "plain-private-key"}}, False),
    ({"credentials": {"ANTHROPIC_API_KEY": "secret://env/KEY\n"}}, False),
    ({"credentials": {"ANTHROPIC_API_KEY": 12}}, False),
    ({"credentials": {**CREDENTIALS, "extra": "private"}}, False),
    ({"credentials": CREDENTIALS, "extra": True}, False),
    ({"credentials": CREDENTIALS, "cli_telemetry": True}, True),
    ({"credentials": CREDENTIALS, "cli_telemetry": 1}, False),
    ({"credentials": CREDENTIALS, "cli_telemetry": "false"}, False),
    ({"credentials": CREDENTIALS, "max_retries": 0}, True),
    ({"credentials": CREDENTIALS, "max_retries": MAX_RETRIES}, True),
    ({"credentials": CREDENTIALS, "max_retries": 2.0}, True),
    ({"credentials": CREDENTIALS, "max_retries": -1}, False),
    ({"credentials": CREDENTIALS, "max_retries": MAX_RETRIES + 1}, False),
    ({"credentials": CREDENTIALS, "max_retries": True}, False),
    ({"credentials": CREDENTIALS, "max_retries": "2"}, False),
    ({"credentials": CREDENTIALS, "max_retries": 1.5}, False),
    ({"credentials": CREDENTIALS, "max_retries": None}, False),
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
)


@pytest.mark.parametrize(("document", "accepted"), SHAPE_CASES)
def test_claude_deployment_refuses_unknown_and_plain_credentials(
    document: JsonObject, accepted: bool
) -> None:
    if accepted:
        parsed = ClaudeDeployment.model_validate(document)

        assert parsed.credentials.ANTHROPIC_API_KEY == CREDENTIALS["ANTHROPIC_API_KEY"]
    else:
        with pytest.raises(ValidationError):
            ClaudeDeployment.model_validate(document)


@pytest.mark.parametrize(("quota", "accepted"), QUOTA_CASES)
def test_claude_deployment_quota_defaults_and_refusals(
    quota: JsonValue, accepted: bool
) -> None:
    document: JsonObject = {"credentials": CREDENTIALS, "quota": quota}

    if accepted:
        parsed = ClaudeDeployment.model_validate(document)

        assert parsed.quota.release_batch >= 1
        assert parsed.quota.release_interval > 0
    else:
        with pytest.raises(ValidationError):
            ClaudeDeployment.model_validate(document)


def test_claude_config_schema_agrees_with_model() -> None:
    schema = ClaudeHarness.config_schema
    cases = (
        *SHAPE_CASES,
        *(
            ({"credentials": CREDENTIALS, "quota": quota}, accepted)
            for quota, accepted in QUOTA_CASES
        ),
    )
    missing_targets: tuple[JsonObject, ...] = (
        {"credentials": CREDENTIALS, "quota": {"vendor_groups": {"window": "missing"}}},
        {"credentials": CREDENTIALS, "quota": {"groups": {"custom": ["*"]}}},
    )
    Draft202012Validator.check_schema(schema)
    validator = cast("SchemaValidator", Draft202012Validator(schema))

    for document, accepted in cases:
        schema_accepts = validator.is_valid(document)
        try:
            ClaudeDeployment.model_validate(document)
        except ValidationError:
            model_accepts = False
        else:
            model_accepts = True

        assert schema_accepts == model_accepts == accepted, document
    for document in missing_targets:
        schema_accepts = validator.is_valid(document)
        with pytest.raises(ValidationError) as rejected:
            ClaudeDeployment.model_validate(document)

        assert schema_accepts
        assert [error["type"] for error in rejected.value.errors()] == [
            "quota_group_unknown"
        ]
    assert schema == CONFIG_SCHEMA


@pytest.mark.parametrize("telemetry", [False, True])
def test_claude_deployment_drives_launch_environment(
    tmp_path: Path, telemetry: bool
) -> None:
    deployment = ClaudeDeployment.model_validate(
        {"credentials": CREDENTIALS, "cli_telemetry": telemetry, "max_retries": 3}
    )

    configuration = ClaudeEnvironmentConfiguration.from_deployment(
        deployment, bash_path="/bin/bash", binding="claude-test"
    )
    inputs = configuration.launch_inputs(cwd=tmp_path, placements=placements(tmp_path))

    assert configuration.binding == "claude-test"
    assert "binding" not in inputs.environment
    assert "claude-test" not in inputs.environment.values()
    assert inputs.environment["CLAUDE_CODE_MAX_RETRIES"] == "3"
    assert ("DISABLE_AUTOUPDATER" in inputs.environment) == telemetry
    assert (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in inputs.environment
    ) != telemetry
    assert "ANTHROPIC_API_KEY" not in inputs.environment
    assert deployment.quota.groups == {
        "account": ("*",),
        "opus": ("claude-opus-*",),
        "sonnet": ("claude-sonnet-*",),
    }
    assert deployment.quota.vendor_groups == {
        "five_hour": "account",
        "seven_day": "account",
        "seven_day_opus": "opus",
        "seven_day_sonnet": "sonnet",
    }
    assert deployment.quota.release_batch == 1
    assert deployment.quota.release_interval == 1.0


def test_claude_deployment_default_launch_settings(tmp_path: Path) -> None:
    deployment = ClaudeDeployment.model_validate({"credentials": CREDENTIALS})

    inputs = ClaudeEnvironmentConfiguration.from_deployment(
        deployment, bash_path="/bin/bash", binding="claude-test"
    ).launch_inputs(cwd=tmp_path, placements=placements(tmp_path))

    assert inputs.environment["CLAUDE_CODE_MAX_RETRIES"] == "2"
    assert inputs.environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "DISABLE_AUTOUPDATER" not in inputs.environment
