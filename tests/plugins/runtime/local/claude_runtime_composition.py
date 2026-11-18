import hashlib
import sys
from pathlib import Path
from typing import cast

from kora.observability import ProcessDiagnostics
from kora.plugins.harness.claude.adapter import ClaudeHarness
from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.harness.claude.session import COMPONENT
from kora.plugins.runtime.local.agent import AgentSources
from kora.plugins.runtime.local.launch import ExpectedBinary
from kora.plugins.runtime.local.supervisor import run
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import AgentExecution, HarnessAdapter
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import SystemClock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import HostedHarness
from kora.runner.calls import CallScope
from kora.testing.runner import AllowDenyPolicies


class ReplayResolver:
    def __init__(self, diagnostics: ProcessDiagnostics) -> None:
        self.diagnostics = diagnostics
        self.scope = SecretScope(diagnostics)

    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness:
        ctx = ProtocolContext(
            namespace_id="launcher-replay",
            run_id=None,
            attempt_id=workspace.attempt_id,
            logger=self.diagnostics.logger(COMPONENT),
            secrets=None,
            secret_admission=self.scope.admission(),
            request_digest=None,
            deadline=None,
            clock=SystemClock(),
            activity=NoActivity(),
        )
        scope = CallScope(
            "claude",
            workspace,
            execution.node_path,
            execution.layers(),
            (),
            execution.output_format,
        )
        adapter = ClaudeHarness(
            scope,
            ctx,
            self.diagnostics.registry,
            ClaudeEnvironmentConfiguration(
                binding="replay",
                bash_path="/bin/bash",
                max_retries=0,
                cli_telemetry=False,
            ),
        )
        return HostedHarness(cast(HarnessAdapter, adapter), adapter.kill)


def sources(diagnostics: ProcessDiagnostics) -> AgentSources:
    target = Path(sys.argv[1])
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    return AgentSources(ReplayResolver(diagnostics), AllowDenyPolicies(), binary=binary)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[2:], sources))
