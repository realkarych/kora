from pathlib import Path

import pytest

from kora.conformance.harness import (
    HarnessCase,
    test_harness_events_are_normalised,
    test_harness_max_events_denies_at_exact_count,
    test_harness_policy_precedes_every_tool_call,
    test_harness_structured_output_and_fallbacks,
    test_harness_usage_completeness,
)
from kora.conformance.harness import kora_capture as kora_capture
from kora.conformance.harness import kora_harness_host as kora_harness_host
from kora.ir import ToolPolicy
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.claude.adapter import ClaudeHarness
from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.policy import PolicyLayers
from kora.protocols.vcs import WorkspaceRef
from kora.runner.calls import CallScope
from tests.plugins.harness.claude.conformance_launch import ContextOwner

__all__ = [
    "test_harness_events_are_normalised",
    "test_harness_max_events_denies_at_exact_count",
    "test_harness_policy_precedes_every_tool_call",
    "test_harness_structured_output_and_fallbacks",
    "test_harness_usage_completeness",
]


def make_adapter(
    workspace: Path,
    ctx: ProtocolContext,
    output_format: bool = False,
    *,
    max_retries: int = 0,
    binding: str = "claude-test",
) -> ClaudeHarness:
    scope = CallScope(
        "claude",
        WorkspaceRef(
            attempt_id="attempt",
            root=str(workspace),
            repo="repository",
            commit="commit",
            base_commit="base",
            branch=None,
        ),
        "conformance",
        PolicyLayers(ToolPolicy(), ToolPolicy()),
        (),
        output_format,
    )
    return ClaudeHarness(
        scope,
        ctx,
        SecretRegistry(),
        ClaudeEnvironmentConfiguration(
            binding=binding,
            bash_path="/bin/bash",
            max_retries=max_retries,
            cli_telemetry=False,
        ),
    )


class Exercise:
    denied_tool = "Forbidden"

    def __init__(self) -> None:
        self.owner = ContextOwner()
        self.context_factory = self.owner.context

    def adapter(self, case: HarnessCase, ctx: ProtocolContext) -> ClaudeHarness:
        assert self.owner.active is not None
        return make_adapter(
            self.owner.active.workspace, ctx, case is HarnessCase.STRUCTURED_OUTPUT
        )

    async def failure(self, ctx: ProtocolContext, marker: str) -> object:
        from kora.ir.config import NodeLimits
        from kora.plugins.harness.claude.options import session_options

        return session_options({marker: True}, NodeLimits())


@pytest.fixture
def kora_harness_exercise() -> Exercise:
    return Exercise()
