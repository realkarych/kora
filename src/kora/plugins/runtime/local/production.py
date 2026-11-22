import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from kora.ir.json import JsonObject
from kora.observability import ProcessDiagnostics
from kora.plugins.harness.claude.adapter import ClaudeHarness, installed_cli_version
from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.harness.claude.deployment import ClaudeDeployment
from kora.plugins.harness.claude.record import recorded_binary
from kora.plugins.harness.claude.session import COMPONENT
from kora.plugins.runtime.local.agent import (
    UNAVAILABLE,
    AgentSources,
    EmptyCredentials,
    HarnessCredentials,
    ProductionResolver,
)
from kora.plugins.runtime.local.channel import delivery_error
from kora.plugins.runtime.local.installation import (
    InstallationConfiguration,
    RunnerRefused,
)
from kora.plugins.runtime.local.launch import ExpectedBinary
from kora.plugins.runtime.local.workspace import CONTEXT_NAMESPACE
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import AgentExecution, HarnessAdapter
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from kora.protocols.policy import Policy
from kora.protocols.secret_value import SecretValue
from kora.protocols.secrets import SecretsProvider
from kora.protocols.time import SystemClock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import HarnessResolver, HostedHarness
from kora.runner.calls import CallScope

BASH_PATH: Final[Path] = Path("/bin/bash")


class ConfiguredPolicies:
    def __init__(
        self, sections: Mapping[str, JsonObject], diagnostics: ProcessDiagnostics
    ) -> None:
        self.configured: Policy | None = None
        if len(sections) == 1:
            name, section = next(iter(sections.items()))
            metadata = load_provider(ProtocolName.POLICY, name, diagnostics)
            factory = cast(Callable[[JsonObject], Policy], metadata.implementation)
            try:
                self.configured = factory(section)
            except (ValidationError, ValueError, TypeError):
                raise RunnerRefused("installation_invalid") from None

    def policy(self, execution: AgentExecution) -> Policy:
        if self.configured is None:
            raise delivery_error(UNAVAILABLE)
        return self.configured


class InstallationCredentials:
    def __init__(
        self,
        reference: str,
        sections: Mapping[str, JsonObject],
        diagnostics: ProcessDiagnostics,
    ) -> None:
        name = reference.removeprefix("secret://").split("/", 1)[0]
        if name not in sections:
            raise RunnerRefused("installation_invalid")
        metadata = load_provider(ProtocolName.SECRETS, name, diagnostics)
        factory = cast(Callable[[JsonObject], SecretsProvider], metadata.implementation)
        try:
            self.provider: SecretsProvider = factory(sections[name])
        except (ValidationError, ValueError, TypeError):
            raise RunnerRefused("installation_invalid") from None
        self.reference = reference

    async def resolve(
        self, execution: AgentExecution, ctx: ProtocolContext
    ) -> Mapping[str, SecretValue]:
        if execution.harness != "claude":
            return {}
        return {"ANTHROPIC_API_KEY": await self.provider.resolve(self.reference, ctx)}


class ClaudeResolver:
    def __init__(
        self,
        section: JsonObject,
        diagnostics: ProcessDiagnostics,
    ) -> None:
        try:
            self.deployment: ClaudeDeployment = ClaudeDeployment.model_validate(section)
        except ValidationError:
            raise RunnerRefused("installation_invalid") from None
        self.diagnostics: ProcessDiagnostics = diagnostics

    @property
    def tested_versions(self) -> str:
        return ClaudeHarness.tested_versions

    def expected_binary(self) -> ExpectedBinary:
        recorded = recorded_binary()
        return ExpectedBinary(recorded.path, recorded.sha256, recorded.size)

    def installed_version(self) -> str:
        return installed_cli_version()

    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness:
        try:
            usable_bash = (
                BASH_PATH.is_absolute()
                and stat.S_ISREG(os.stat(BASH_PATH).st_mode)
                and os.access(BASH_PATH, os.X_OK)
            )
        except OSError:
            raise delivery_error(UNAVAILABLE) from None
        if not usable_bash:
            raise delivery_error(UNAVAILABLE)
        scope = CallScope(
            "claude",
            workspace,
            execution.node_path,
            execution.layers(),
            (),
            execution.output_format,
        )
        ctx = ProtocolContext(
            namespace_id=CONTEXT_NAMESPACE,
            run_id=None,
            attempt_id=workspace.attempt_id,
            logger=self.diagnostics.logger(COMPONENT).bind(
                attempt_id=workspace.attempt_id
            ),
            secrets=None,
            secret_admission=None,
            request_digest=None,
            deadline=None,
            clock=SystemClock(),
            activity=NoActivity(),
        )
        environment = ClaudeEnvironmentConfiguration.from_deployment(
            self.deployment, bash_path=str(BASH_PATH), binding="claude"
        )
        adapter = ClaudeHarness(scope, ctx, self.diagnostics.registry, environment)
        return HostedHarness(cast(HarnessAdapter, adapter), adapter.kill)


def production_harnesses(
    diagnostics: ProcessDiagnostics,
    installation: InstallationConfiguration,
) -> Mapping[str, ClaudeResolver]:
    if "claude" not in installation.harness:
        return {}
    return {"claude": ClaudeResolver(installation.harness["claude"], diagnostics)}


def production_sources(
    diagnostics: ProcessDiagnostics,
    installation: InstallationConfiguration,
) -> AgentSources:
    harnesses = production_harnesses(diagnostics, installation)
    hosted: dict[str, HarnessResolver] = dict(harnesses)
    binary: ExpectedBinary | None = None
    credentials: HarnessCredentials = EmptyCredentials()
    if "claude" in harnesses:
        resolver = harnesses["claude"]
        credentials = InstallationCredentials(
            resolver.deployment.credentials.ANTHROPIC_API_KEY,
            installation.secrets,
            diagnostics,
        )
        binary = resolver.expected_binary()
    return AgentSources(
        ProductionResolver(hosted),
        ConfiguredPolicies(installation.policy, diagnostics),
        binary=binary,
        credentials=credentials,
    )
