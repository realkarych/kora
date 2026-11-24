import sys

from kora.observability import ProcessDiagnostics
from kora.plugins.harness.fake import CallScope, FakeHarness
from kora.plugins.harness.fake.adapter import COMPONENT
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.plugins.runtime.local.agent import AgentSources
from kora.plugins.runtime.local.channel import delivery_error
from kora.plugins.runtime.local.installation import InstallationConfiguration
from kora.plugins.runtime.local.supervisor import run
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import AgentExecution
from kora.protocols.policy import Policy
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import SystemClock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import HostedHarness
from kora.runner.session_result import FinalizationImplementation
from kora.testing.script import ScriptIntake


class AllowDenyPolicies:
    def policy(self, execution: AgentExecution) -> Policy:
        return AllowDenyPolicy()


class ScriptResolver:
    def __init__(self, intake: ScriptIntake, ctx: ProtocolContext) -> None:
        self.intake = intake
        self.ctx = ctx

    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness:
        source = self.intake.script
        if source is None:
            raise delivery_error("workload_kind_unavailable")
        scope = CallScope(
            harness=execution.harness,
            workspace=workspace,
            node_path=execution.node_path,
            layers=execution.layers(),
            protected_paths=(),
            output_format=execution.output_format,
        )
        adapter = FakeHarness(source, scope, self.ctx)
        return HostedHarness(
            adapter,
            adapter.kill,
            FinalizationImplementation(name="fake", version="fake-1"),
        )


def testing_sources(
    diagnostics: ProcessDiagnostics, installation: InstallationConfiguration
) -> AgentSources:
    ctx = ProtocolContext(
        namespace_id="testing-runner",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger(COMPONENT),
        secrets=None,
        secret_admission=SecretScope(diagnostics).admission(),
        request_digest=None,
        deadline=None,
        clock=SystemClock(),
        activity=NoActivity(),
    )
    intake = ScriptIntake()
    return AgentSources(ScriptResolver(intake, ctx), AllowDenyPolicies(), intake)


def main() -> None:
    sys.exit(run(sys.argv[1:], testing_sources))


if __name__ == "__main__":
    main()
