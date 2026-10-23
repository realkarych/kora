import json
import re
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import MISSING, ValidationError

from kora.ir import TimerConfig
from tests.ir.support import load_yaml, schema_definition


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"duration": "0s", "at": "2024-01-01T00:00:00Z"},
        {"duration": None},
        {"at": None},
        {"at": 0},
        {"at": datetime(2024, 1, 1)},
        {"duration": "{{delay}}"},
        {"unknown": "value"},
    ],
)
def test_timer_config_enforces_presence_exclusivity(
    document: dict[str, object],
) -> None:
    model = TimerConfig

    with pytest.raises(ValidationError):
        model.model_validate(document)


@pytest.mark.parametrize("value", ["0s", "0001ms", "1" * 10000 + "d"])
def test_timer_duration_is_not_coerced_or_capped(value: str) -> None:
    model = TimerConfig
    document = {"duration": value}

    parsed = model.model_validate(document)

    assert parsed.model_dump(exclude_unset=True) == document


TIMESTAMPS: list[tuple[str, bool]] = [
    ("0001-01-01T00:00:00Z", True),
    ("9999-12-31t23:59:59z", True),
    ("2000-02-29T01:02:03.1+23:59", True),
    ("2024-02-29t23:59:59.120000-00:00", True),
    ("2024-01-01T00:00:00.123456Z", True),
    ("0000-01-01T00:00:00Z", False),
    ("1900-02-29T00:00:00Z", False),
    ("2024-04-31T00:00:00Z", False),
    ("2024-01-01T24:00:00Z", False),
    ("2024-01-01T00:60:00Z", False),
    ("2024-01-01T00:00:60Z", False),
    ("2024-01-01T00:00:00+24:00", False),
    ("2024-01-01T00:00:00+00:60", False),
    ("2024-01-01T00:00:00.Z", False),
    ("2024-01-01T00:00:00.1234567Z", False),
    ("2024-01-01T00:00:00,1Z", False),
    ("2024-01-01T00:00:00", False),
    ("2024-01-01 00:00:00Z", False),
    ("２０２４-01-01T00:00:00Z", False),
    ("2024-01-01T00:00:00Z\n", False),
    ("2024-01-01T00:00:00Z ", False),
]


@pytest.mark.parametrize(("value", "valid"), TIMESTAMPS)
def test_timer_timestamp_profile_matches_schema_consumers(
    value: str, valid: bool
) -> None:
    model = TimerConfig
    schema = cast("dict[str, object]", schema_definition("TimerConfig"))
    document = {"at": value}

    model_valid = True
    try:
        model.model_validate(document)
    except ValidationError:
        model_valid = False

    assert model_valid is valid
    properties = cast("dict[str, object]", schema["properties"])
    timestamp = cast("dict[str, str]", properties["at"])
    if valid:
        assert re.fullmatch(timestamp["pattern"], value) is not None
    assert timestamp["format"] == "date-time"


def test_timer_config_preserves_authored_text() -> None:
    model = TimerConfig
    document = {"at": "2024-02-29t23:59:59.120000-00:00", "x-data": False}

    parsed = model.model_validate(document)

    assert parsed.model_dump(mode="json", exclude_unset=True) == document


def test_ir_yaml_timestamp_tokens_remain_strings(tmp_path: Path) -> None:
    path = tmp_path / "timestamp.yaml"
    path.write_text("at: 2024-02-29T23:59:59.120000-00:00\n")

    document = load_yaml(path)

    assert document == {"at": "2024-02-29T23:59:59.120000-00:00"}


def test_agent_config_preserves_omission_and_explicit_values() -> None:
    from kora.ir import AgentConfig, export_ir
    from tests.ir.remaining_config_cases import MINIMAL, POPULATED

    documents: list[dict[str, object]] = [
        MINIMAL[AgentConfig],
        POPULATED[AgentConfig],
        {**MINIMAL[AgentConfig], "skills": [], "hooks": {}, "mcp_servers": {}},
    ]

    records = [AgentConfig.model_validate(document) for document in documents]

    assert [export_ir(record) for record in records] == documents
    assert records[0].model is MISSING


@pytest.mark.parametrize(
    "field", ["name", "description", "prompt", "tools", "unknown", "extensions"]
)
def test_subagent_records_enforce_approved_fields(field: str) -> None:
    from kora.ir import SubagentConfig

    document = {"name": "", "description": "", "prompt": "", field: False}

    with pytest.raises(ValidationError):
        SubagentConfig.model_validate(document)


def test_agent_skills_are_unique_without_reordering() -> None:
    from kora.ir import AgentConfig, export_ir
    from tests.ir.remaining_config_cases import MINIMAL

    document = {**MINIMAL[AgentConfig], "skills": ["z", "a", ""]}

    parsed = AgentConfig.model_validate(document)
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**document, "skills": ["z", "a", "z"]})

    assert export_ir(parsed) == document


def test_remaining_records_round_trip_minimal_populated_and_refuse_invalid() -> None:
    from kora.ir import export_ir
    from tests.ir.remaining_config_cases import INVALID, MINIMAL, POPULATED

    documents = [*MINIMAL.items(), *POPULATED.items()]

    exported = [
        export_ir(model.model_validate(document)) for model, document in documents
    ]
    for model, document in INVALID.items():
        with pytest.raises(ValidationError):
            model.model_validate(document)

    assert exported == [document for _, document in documents]
    assert [json.loads(json.dumps(value)) for value in exported] == exported


@pytest.mark.parametrize(
    ("model_name", "field", "value"),
    [
        ("AgentConfig", "harness", "claude\n"),
        ("AgentConfig", "harness", "acp:"),
        ("AgentConfig", "skills", ["a", "a"]),
        ("AgentConfig", "skills", [False]),
        (
            "AgentConfig",
            "subagents",
            [{"name": "", "description": "", "prompt": "", "vendor": True}],
        ),
        ("AgentConfig", "mcp_servers", {"x-name": {"type": "wrong"}}),
        ("AgentConfig", "env", {"x-name": "value"}),
        ("AgentConfig", "secret_env", {"TOKEN": "alias"}),
        ("AgentConfig", "permission_mode", "default"),
        ("AgentConfig", "sandbox", "unsafe"),
        ("AgentConfig", "trust_repo_config", 0),
        ("HumanInputBinds", "gate_digests", ["a" * 4097]),
        ("HumanInputBinds", "vcs_ref", "a" * 4097),
        ("HumanInputConfig", "on_expiry", "approve"),
        ("HumanInputConfig", "approve_when", ""),
        ("HumanInputConfig", "context", "a" * 4097),
        ("EventSelector", "type", "sensor"),
        ("EventSelector", "match", False),
        ("SensorPoll", "sensor", "http\n"),
        ("SensorPoll", "until", "a" * 4097),
        ("WaitEventConfig", "poll", {"sensor": "http", "until": "true"}),
        ("WaitEventConfig", "interval", "1s\n"),
    ],
)
def test_remaining_config_fields_refuse_invalid_contract_values(
    model_name: str, field: str, value: object
) -> None:
    from tests.ir.remaining_config_cases import MINIMAL

    model = next(model for model in MINIMAL if model.__name__ == model_name)
    document = {**MINIMAL[model], field: value}

    with pytest.raises(ValidationError):
        model.model_validate(document)


def test_remaining_optional_fields_refuse_explicit_null() -> None:
    from tests.ir.remaining_config_cases import MINIMAL, POPULATED

    cases = [
        (model, {**MINIMAL[model], name: None})
        for model, document in POPULATED.items()
        for name in document
        if name != "default_value" and not name.startswith("x-")
    ]

    for model, document in cases:
        with pytest.raises(ValidationError):
            model.model_validate(document)


def test_remaining_configs_preserve_empty_subagents_and_poll_interval_omission() -> (
    None
):
    from kora.ir import AgentConfig, SubagentConfig, WaitEventConfig, export_ir
    from tests.ir.remaining_config_cases import MINIMAL

    helper = SubagentConfig(name="", description="", prompt="", tools=("Read", "Read"))
    document: dict[str, object] = {
        **MINIMAL[AgentConfig],
        "subagents": [helper, helper],
        "hooks": {"v": []},
        "output_schema": {"v": []},
    }

    parsed = AgentConfig.model_validate(document)
    poll = WaitEventConfig.model_validate(
        {
            "poll": {"sensor": "http", "until": "true"},
            "timeout": "0s",
            "on_expiry": "fail",
        }
    )

    assert (
        export_ir(parsed)["subagents"]
        == [{"name": "", "description": "", "prompt": "", "tools": ["Read", "Read"]}]
        * 2
    )
    assert poll.interval is MISSING
