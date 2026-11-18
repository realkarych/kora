import pytest

from kora.conformance.harness import CONFORMANCE_PRICE_TABLE
from kora.ir.config import NodeLimits
from kora.ir.json import JsonObject
from kora.plugins.harness.codex.adapter import CodexHarness
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import SessionInputs, prompt_digest
from kora.protocols.vcs import WorkspaceTarget
from kora.runner.calls import TRACKING_CEILING


def session_inputs(limits: NodeLimits) -> SessionInputs:
    return SessionInputs(
        "conformance",
        prompt_digest("conformance"),
        CONFORMANCE_PRICE_TABLE.id,
        CONFORMANCE_PRICE_TABLE,
        limits,
    )


@pytest.mark.parametrize(
    ("limits", "expected_max_events"),
    [
        (NodeLimits(max_events=1), 1),
        (NodeLimits(max_events=3), 3),
        (NodeLimits(max_events=100000), 100000),
        (NodeLimits(), TRACKING_CEILING),
    ],
)
def test_codex_session_spec_preserves_sealed_inputs(
    limits: NodeLimits, expected_max_events: int
) -> None:
    adapter = CodexHarness()
    workspace = WorkspaceTarget(root="/workspace", host_path=None)
    inputs = session_inputs(limits)
    config: JsonObject = {
        "prompt": "ignored",
        "permission_mode": "plan",
        "trust_repo_config": False,
        "model": "gpt-5.1-codex",
        "system_prompt_append": "extra",
        "output_schema": {"type": "object"},
        "harness": "codex",
        "workspace": "repository",
        "tool_policy": "default",
        "env": {},
        "secret_env": {},
        "runtime": "local",
        "vcs": "git",
    }

    spec = adapter.build_session_spec(config, workspace, None, inputs=inputs)

    assert spec.harness == "codex"
    assert spec.adapter_version == "0.166.0"
    assert spec.prompt == "conformance"
    assert spec.price_table_id == CONFORMANCE_PRICE_TABLE.id
    assert spec.price_table == CONFORMANCE_PRICE_TABLE.model_dump(mode="json")
    assert spec.files == ()
    assert spec.resume_handle is None
    assert spec.options == {
        "model": "gpt-5.1-codex",
        "system_prompt_append": "extra",
        "permission_mode": "plan",
        "max_events": expected_max_events,
        "output_schema": {"type": "object"},
    }


def test_codex_session_spec_defaults_permission_mode_to_dont_ask() -> None:
    adapter = CodexHarness()
    workspace = WorkspaceTarget(root="/workspace", host_path=None)
    inputs = session_inputs(NodeLimits())

    spec = adapter.build_session_spec({}, workspace, None, inputs=inputs)

    assert spec.options == {
        "model": None,
        "system_prompt_append": None,
        "permission_mode": "dont_ask",
        "max_events": TRACKING_CEILING,
        "output_schema": None,
    }


@pytest.mark.parametrize(
    "config",
    [
        {"skills": True},
        {"subagents": True},
        {"hooks": True},
        {"mcp_servers": True},
        {"sandbox": True},
        {"network_policy": True},
        {"unknown_field": True},
        {"trust_repo_config": True},
    ],
)
def test_codex_session_spec_refuses_unsupported_configuration(
    config: JsonObject,
) -> None:
    adapter = CodexHarness()
    workspace = WorkspaceTarget(root="/workspace", host_path=None)
    inputs = session_inputs(NodeLimits())

    with pytest.raises(KoraProtocolError) as raised:
        adapter.build_session_spec(config, workspace, None, inputs=inputs)

    assert raised.value.code == "config_error"
    assert raised.value.details == {}
    assert raised.value.message == "the Codex session configuration is unsupported"


@pytest.mark.parametrize(
    ("config", "limits"),
    [
        ({"model": "bad name!"}, NodeLimits()),
        ({"model": "gpt-5\n"}, NodeLimits()),
        ({"model": "a" * 129}, NodeLimits()),
        ({"permission_mode": "never"}, NodeLimits()),
        ({"output_schema": "not-an-object"}, NodeLimits()),
        ({}, NodeLimits(budget_usd=1)),
    ],
)
def test_codex_session_spec_refuses_invalid_values(
    config: JsonObject, limits: NodeLimits
) -> None:
    adapter = CodexHarness()
    workspace = WorkspaceTarget(root="/workspace", host_path=None)
    inputs = session_inputs(limits)

    with pytest.raises(KoraProtocolError) as raised:
        adapter.build_session_spec(config, workspace, None, inputs=inputs)

    assert raised.value.code == "config_error"
    assert raised.value.details == {}
    assert raised.value.message == "the Codex session configuration is unsupported"
