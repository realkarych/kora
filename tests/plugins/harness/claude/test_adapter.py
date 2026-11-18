from pathlib import Path

import pytest
from claude_agent_sdk import __version__
from claude_agent_sdk._cli_version import __cli_version__
from packaging.specifiers import SpecifierSet

from kora.conformance.capture import Capture
from kora.ir import ErrorClass
from kora.ir.json import JsonObject
from kora.plugins.harness.claude.adapter import ClaudeSummary
from kora.plugins.harness.claude.configuration import CLI_VERSION, SDK_VERSION
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import RunnerCause, RunnerOutcome
from kora.protocols.time import SystemClock
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import (
    claude_capture as claude_capture,
)
from tests.plugins.harness.claude.test_replay_events import (
    context_for,
    init_fields,
    projector,
)


@pytest.mark.parametrize("known_first", [False, True])
def test_finalise_retains_unattributed_usage_revision(
    claude_capture: Capture,
    known_first: bool,
) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    _sink, events = projector()
    events.observe_init(init_fields())
    if known_first:
        events.usage_start("known", "claude-sonnet-4-5", None, {"input_tokens": 1})
        events.usage_stop()
    events.usage_start("unknown-model", None, None, {"input_tokens": 3})
    events.usage_delta({"output_tokens": 2})
    events.usage_stop()
    events.observe_result("success", False, None, None)
    events.finish_session()

    result = adapter.finalise(
        ClaudeSummary("claude", events),
        RunnerOutcome("completed", "teardown_after_result", 0, None),
    )

    assert len(result.usage) == int(known_first)
    assert result.usage_coverage.completeness == (
        "partial" if known_first else "unavailable"
    )
    assert result.usage_coverage.attribution_gap
    assert result.usage_coverage.observation_id == "unknown-model"
    assert result.usage_coverage.observation_sequence == int(known_first)
    assert result.usage_coverage.observed_at is not None


@pytest.mark.parametrize(
    ("cause", "subtype", "expected"),
    [
        ("completed", "success", None),
        ("deadline", "success", ErrorClass.TIMEOUT),
        ("completed", "error_max_turns", ErrorClass.EVENTS_EXHAUSTED),
    ],
)
def test_finalise_preserves_failure_before_output_validation(
    claude_capture: Capture,
    cause: RunnerCause,
    subtype: str,
    expected: ErrorClass | None,
) -> None:
    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    _sink, events = projector()
    events.observe_init(init_fields())
    events.observe_result(
        subtype,
        subtype != "success",
        None,
        None,
        structured_output=float("nan"),
        result='{"fallback":true}',
    )
    events.finish_session()
    summary = ClaudeSummary("claude", events)
    outcome = RunnerOutcome(cause, "teardown_after_result", 0, None)

    result = adapter.finalise(summary, outcome)

    assert result.error_class is expected
    assert result.output is None
    assert result.output_diagnostic == (
        "structured_output_invalid" if expected is None else None
    )


@pytest.mark.parametrize(
    "key",
    [
        "skills",
        "subagents",
        "mcp_servers",
        "sandbox",
        "network_policy",
        "hooks",
        "unknown",
        "trust_repo_config",
    ],
)
def test_session_spec_refuses_unsupported_configuration(
    claude_capture: Capture, key: str
) -> None:
    from tests.plugins.harness.claude.test_session_lifecycle import session_spec

    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))

    with pytest.raises(KoraProtocolError) as error:
        session_spec(adapter, config={key: True})

    assert error.value.code == "config_error"
    assert error.value.details == {}


@pytest.mark.parametrize("maximum", [1, 3, 100000])
def test_session_spec_preserves_sealed_inputs(
    claude_capture: Capture, maximum: int
) -> None:
    from kora.conformance.harness import CONFORMANCE_PRICE_TABLE
    from kora.ir.config import NodeLimits
    from tests.plugins.harness.claude.test_session_lifecycle import session_spec

    adapter = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))

    spec = session_spec(
        adapter,
        NodeLimits(max_events=maximum),
        {"prompt": "ignored", "permission_mode": "plan", "trust_repo_config": False},
    )

    assert spec.prompt == "conformance"
    assert spec.options["max_turns"] == maximum
    assert spec.options["permission_mode"] == "plan"
    assert spec.price_table == CONFORMANCE_PRICE_TABLE.model_dump(mode="json")
    assert spec.files == ()
    assert spec.resume_handle is None


def test_claude_policy_coverage_matches_section_12(claude_capture: Capture) -> None:
    harness = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))

    capabilities = harness.capabilities()

    assert capabilities.policy_coverage == {
        "shell",
        "file_edit",
        "mcp_tool",
        "subagent",
        "hosted_tool",
    }
    assert not capabilities.resume
    assert capabilities.reports_usage_per_turn
    assert harness.name == "claude"
    assert harness.egress_hosts == ("api.anthropic.com",)


def test_claude_installed_version_matches_pinned_sdk(claude_capture: Capture) -> None:
    harness = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    tested = SpecifierSet(harness.tested_versions)

    installed = harness.installed_version()

    assert installed == __cli_version__ == CLI_VERSION
    assert __version__ == SDK_VERSION
    assert installed in tested
    assert str(tested) == f"=={CLI_VERSION}"
    assert "2.1.300" not in tested
    assert "2.1.302" not in tested


def test_claude_check_config_warns_only_for_hooks(claude_capture: Capture) -> None:
    harness = make_adapter(Path("/work"), context_for(claude_capture, SystemClock()))
    supported: JsonObject = {
        "prompt": "task",
        "model": "claude-sonnet-4-5",
        "system_prompt_append": "instruction",
        "skills": [],
        "subagents": [],
        "mcp_servers": {},
        "tool_policy": {},
        "permission_mode": "dont_ask",
        "sandbox": "workspace_write",
        "trust_repo_config": False,
        "network_policy": {},
        "output_schema": {},
        "env": {},
        "secret_env": {},
    }

    accepted = harness.check_config(supported)
    warned = harness.check_config(
        {**supported, "hooks": {"private-hook": "private-content"}}
    )
    empty = harness.check_config({"hooks": {}})

    assert accepted == []
    assert len(warned) == 1
    assert warned[0].code == "IR008"
    assert warned[0].path == ("hooks",)
    assert "private-" not in warned[0].message
    assert empty == warned
