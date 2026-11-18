from packaging.specifiers import SpecifierSet

from kora.ir.json import JsonObject
from kora.plugins.harness.codex.adapter import CodexHarness
from kora.plugins.harness.codex.deployment import CONFIG_SCHEMA
from kora.plugins.harness.codex.reservations import RESERVED_ENV


def test_codex_declarations_match_sections_12_and_14() -> None:
    harness = CodexHarness()

    capabilities = harness.capabilities()
    versions = SpecifierSet(harness.tested_versions)

    assert harness.name == "codex"
    assert harness.egress_hosts == ("api.openai.com",)
    assert str(versions) == "==0.166.0"
    assert "0.166.0" in versions
    assert "0.165.0" not in versions
    assert "0.167.0" not in versions
    assert capabilities.resume is False
    assert capabilities.reports_usage_per_turn is True
    assert capabilities.policy_coverage == frozenset({"shell", "file_edit"})
    assert harness.config_schema is CONFIG_SCHEMA
    assert harness.reserved_env is RESERVED_ENV


def test_codex_check_config_warns_only_for_subagents() -> None:
    harness = CodexHarness()
    supported: JsonObject = {
        "prompt": "task",
        "model": "fixture/model:2026",
        "system_prompt_append": "instruction",
        "skills": [],
        "hooks": {"private-hook": "private-content"},
        "mcp_servers": {},
        "tool_policy": {},
        "permission_mode": "dont_ask",
        "sandbox": "workspace_write",
        "trust_repo_config": False,
        "network_policy": {},
        "output_schema": {},
        "env": {},
        "secret_env": {},
        "limits": {
            "stall_after": "1m",
            "timeout": "5m",
            "max_events": 10,
            "budget_usd": "1",
        },
    }

    accepted = harness.check_config(supported)
    warned = harness.check_config(
        {**supported, "subagents": [{"name": "private-agent"}]}
    )
    empty = harness.check_config({"subagents": []})

    assert accepted == []
    assert len(warned) == 1
    assert warned[0].code == "IR008"
    assert warned[0].path == ("subagents",)
    assert warned[0].message == "the Codex harness does not support node subagents"
    assert empty == warned
