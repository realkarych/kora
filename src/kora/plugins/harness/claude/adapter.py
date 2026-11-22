import asyncio
from dataclasses import dataclass
from typing import ClassVar, Final

from kora.ir import ConfigWarning
from kora.ir.cost import UsageCoverage
from kora.ir.json import JsonObject
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.claude.configuration import (
    CLI_VERSION,
    ClaudeEnvironmentConfiguration,
)
from kora.plugins.harness.claude.deployment import CONFIG_SCHEMA
from kora.plugins.harness.claude.events import ClaudeEvents, TextOutcome
from kora.plugins.harness.claude.reservations import RESERVED_ENV
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EventSink,
    EventSummary,
    HarnessCapabilities,
    LaunchContext,
    RunnerOutcome,
    SessionInputs,
    SessionResult,
    SessionSpec,
)
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.policy import PolicyClient
from kora.protocols.vcs import WorkspaceTarget
from kora.runner.calls import CallScope
from kora.runner.outcome import select_output

VERSION: Final[str] = "1"


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.HARNESS, "claude", ClaudeHarness, CONFIG_SCHEMA, VERSION
    )


@dataclass(frozen=True)
class ClaudeSummary:
    harness: str
    events: ClaudeEvents


def output_diagnostic(output_format: bool, events: ClaudeEvents) -> str | None:
    if events.output_refused:
        return "structured_output_invalid"
    if select_output(events.structured, events.result_text) is not None:
        return None
    if not output_format:
        return None
    if events.text_outcome is TextOutcome.INVALID:
        return "output_invalid_json"
    return "output_missing"


def installed_cli_version() -> str:
    from claude_agent_sdk import __version__
    from claude_agent_sdk._cli_version import __cli_version__

    from kora.plugins.harness.claude.sdk import require_native_versions

    require_native_versions(__version__, __cli_version__)
    return __cli_version__


class ClaudeHarness:
    name: ClassVar[str] = "claude"
    config_schema: ClassVar[JsonObject] = CONFIG_SCHEMA
    egress_hosts: ClassVar[tuple[str, ...]] = ("api.anthropic.com",)
    tested_versions: ClassVar[str] = f"=={CLI_VERSION}"
    reserved_env: ClassVar[tuple[str, ...]] = RESERVED_ENV

    def __init__(
        self,
        scope: CallScope,
        ctx: ProtocolContext,
        registry: SecretRegistry,
        environment: ClaudeEnvironmentConfiguration,
    ) -> None:
        self.scope = scope
        self.ctx = ctx
        self.registry = registry
        self.environment = environment
        self.killed = asyncio.Event()

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            resume=False,
            reports_usage_per_turn=True,
            policy_coverage=frozenset(
                {"shell", "file_edit", "mcp_tool", "subagent", "hosted_tool"}
            ),
        )

    def check_config(self, config: JsonObject) -> list[ConfigWarning]:
        if "hooks" not in config:
            return []
        return [
            ConfigWarning(
                code="IR008",
                message="the Claude harness does not support node hooks",
                path=("hooks",),
            )
        ]

    def installed_version(self) -> str:
        return installed_cli_version()

    def build_session_spec(
        self,
        config: JsonObject,
        workspace: WorkspaceTarget,
        feedback: None,
        *,
        inputs: SessionInputs,
    ) -> SessionSpec:
        from kora.plugins.harness.claude.options import session_options

        options = session_options(config, inputs.limits)
        return SessionSpec.model_validate(
            {
                "harness": self.name,
                "adapter_version": CLI_VERSION,
                "options": options.model_dump(mode="json"),
                "files": (),
                "prompt": inputs.prompt,
                "resume_handle": None,
                "price_table_id": inputs.price_table_id,
                "price_table": inputs.price_table.model_dump(mode="json"),
            }
        )

    def kill(self) -> None:
        self.killed.set()

    async def run(
        self,
        spec: SessionSpec,
        policy: PolicyClient,
        sink: EventSink,
        launches: LaunchContext,
    ) -> SessionResult:
        from kora.plugins.harness.claude.run import ClaudeSession
        from kora.plugins.harness.claude.session import SESSION_FINISHED

        events = ClaudeEvents(
            sink,
            self.registry,
            binding=self.environment.binding,
            max_retries=self.environment.max_retries,
            output_format=self.scope.output_format,
            check_deadline=lambda: None,
            observed_at=self.ctx.clock.now,
        )
        summary = ClaudeSummary(spec.harness, events)
        session = ClaudeSession(self.scope, self.ctx, events, sink, policy, self.killed)
        outcome = await session.run(spec, launches, self.environment)
        self.ctx.logger.emit(SESSION_FINISHED, outcome=outcome.cause)
        return self.finalise(summary, outcome)

    def finalise(self, summary: EventSummary, outcome: RunnerOutcome) -> SessionResult:
        if not isinstance(summary, ClaudeSummary):
            raise KoraProtocolError(
                "summary_foreign", "the summary belongs to another adapter", False, {}
            )
        events = summary.events
        facts = events.facts
        observation = events.observation()
        usage = observation.usage
        observed = (
            usage.rows
            if events.latest_usage is None
            else (*usage.rows, events.latest_usage)
        )
        latest = max(observed, key=lambda row: row.observation_sequence, default=None)
        error_class = observation.error_class(outcome.cause)
        return SessionResult(
            harness=summary.harness,
            session_id=None if facts is None else facts.session_id,
            installed_version=None if facts is None else facts.harness_version,
            billing_class=None if facts is None else facts.billing_class,
            stop_reason=outcome.cause,
            terminal_reason=observation.terminal_reason,
            api_error_status=observation.api_error_status,
            error_class=error_class,
            error_summary=None,
            output=select_output(events.structured, events.result_text)
            if error_class is None and not events.output_refused
            else None,
            output_diagnostic=output_diagnostic(self.scope.output_format, events)
            if error_class is None
            else None,
            usage=usage.rows,
            usage_coverage=UsageCoverage(
                version=1,
                observation_id=None if latest is None else latest.observation_id,
                observation_sequence=None
                if latest is None
                else latest.observation_sequence,
                observed_at=None if latest is None else latest.observed_at,
                completeness=usage.completeness,
                attribution_gap=usage.attribution_gap,
                observation_gap=usage.observation_gap,
            ),
            turns=events.turns,
            denials=observation.denials,
            denials_complete=observation.denials_complete,
            unconfirmed_calls=observation.bypass.unconfirmed,
            policy_bypass=observation.bypass.confirmed,
        )
