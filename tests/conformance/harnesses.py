from collections.abc import Awaitable, Callable

import kora.conformance.harness as harness_cases
from kora.conformance.capture import Capture
from kora.conformance.harness import HarnessCase, HarnessHost
from kora.ir import ConfigWarning
from kora.ir.json import JsonObject
from kora.plugins.harness.fake import FakeHarness
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import (
    Enforcement,
    EventSink,
    EventSummary,
    HarnessAdapter,
    HarnessCapabilities,
    LaunchContext,
    RunnerCause,
    RunnerOutcome,
    SessionResult,
    SessionSpec,
)
from kora.protocols.policy import (
    PolicyClient,
)
from kora.protocols.vcs import WorkspaceTarget
from tests.plugins.harness.fake.fake_support import (
    DENIED_TOOL,
    AllowingPolicy,
    FakeExercise,
)

Damage = Callable[[SessionResult], SessionResult]


class UnlimitedSink:
    def __init__(self, sink: EventSink) -> None:
        self.sink = sink

    def emit(self, kind: str, data: JsonObject) -> None:
        self.sink.emit(kind, data)

    def observe_dropped(self, kind: str) -> None:
        self.sink.observe_dropped(kind)

    def enforcement(self) -> Enforcement:
        return Enforcement("continue", None)

    async def termination(self) -> RunnerCause:
        return await self.sink.termination()


class BrokenHarness:
    def __init__(
        self,
        inner: FakeHarness,
        skips_policy: bool = False,
        ignores_limit: bool = False,
        damage: Damage | None = None,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.config_schema = inner.config_schema
        self.egress_hosts = inner.egress_hosts
        self.tested_versions = inner.tested_versions
        self.reserved_env = inner.reserved_env
        self.skips_policy = skips_policy
        self.ignores_limit = ignores_limit
        self.damage = damage

    def capabilities(self) -> HarnessCapabilities:
        return self.inner.capabilities()

    def check_config(self, config: JsonObject) -> list[ConfigWarning]:
        return self.inner.check_config(config)

    def build_session_spec(
        self, config: JsonObject, workspace: WorkspaceTarget, feedback: None
    ) -> SessionSpec:
        return self.inner.build_session_spec(config, workspace, feedback)

    async def run(
        self,
        spec: SessionSpec,
        policy: PolicyClient,
        sink: EventSink,
        launches: LaunchContext,
    ) -> SessionResult:
        chosen: PolicyClient = AllowingPolicy() if self.skips_policy else policy
        hosted: EventSink = UnlimitedSink(sink) if self.ignores_limit else sink
        outcome = await self.inner.run(spec, chosen, hosted, launches)
        return outcome if self.damage is None else self.damage(outcome)

    def finalise(self, summary: EventSummary, outcome: RunnerOutcome) -> SessionResult:
        return self.inner.finalise(summary, outcome)

    def installed_version(self) -> str:
        return self.inner.installed_version()


class BrokenExercise:
    denied_tool: str = DENIED_TOOL

    def __init__(
        self,
        skips_policy: bool = False,
        ignores_limit: bool = False,
        damage: Damage | None = None,
    ) -> None:
        self.fake = FakeExercise()
        self.skips_policy = skips_policy
        self.ignores_limit = ignores_limit
        self.damage = damage

    def failure(self, ctx: ProtocolContext, marker: str) -> Awaitable[object]:
        return self.fake.failure(ctx, marker)

    def adapter(self, case: HarnessCase, ctx: ProtocolContext) -> HarnessAdapter:
        return BrokenHarness(
            self.fake.adapter(case, ctx),
            self.skips_policy,
            self.ignores_limit,
            self.damage,
        )


def without_output(outcome: SessionResult) -> SessionResult:
    return outcome.model_copy(update={"output": None})


def without_usage(outcome: SessionResult) -> SessionResult:
    return outcome.model_copy(update={"usage": ()})


def policy_skipping(capture: Capture) -> None:
    harness_cases.test_harness_policy_precedes_every_tool_call(
        BrokenExercise(skips_policy=True), HarnessHost(), capture
    )


def limit_ignoring(capture: Capture) -> None:
    harness_cases.test_harness_max_events_denies_at_exact_count(
        BrokenExercise(ignores_limit=True), HarnessHost(), capture
    )


def output_dropping(capture: Capture) -> None:
    harness_cases.test_harness_structured_output_and_fallbacks(
        BrokenExercise(damage=without_output), HarnessHost(), capture
    )


def usage_dropping(capture: Capture) -> None:
    harness_cases.test_harness_usage_completeness(
        BrokenExercise(damage=without_usage), HarnessHost(), capture
    )


HARNESS_BROKEN_CASES: tuple[tuple[Callable[[Capture], None], str], ...] = (
    (policy_skipping, "the session asks the policy for its tool calls"),
    (limit_ignoring, "exactly max_events limited events are emitted"),
    (output_dropping, "the structured output channel is selected"),
    (usage_dropping, "reported usage yields model rows"),
)
