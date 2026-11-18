import asyncio
import os
from pathlib import Path

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.harness import (
    HarnessCase,
    HarnessConformanceInputs,
    HarnessHost,
    run_harness_case,
)
from kora.ir.json import JsonObject
from kora.plugins.harness.fake import ListedScript, ScriptHeader
from kora.plugins.harness.fake.script import STEP_ADAPTER
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import (
    EventSink,
    HarnessAdapter,
    LaunchContext,
    LaunchInputs,
    SessionInputs,
    SessionResult,
    SessionSpec,
)
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.policy import (
    PolicyClient,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.protocols.vcs import WorkspaceTarget

from .configured_harness import ConfiguredHarness
from .launching_context import ContextOwner, FixtureLaunch


class ExecutingPolicy:
    def __init__(
        self, policy: PolicyClient, launch: FixtureLaunch, defective: bool
    ) -> None:
        self.policy = policy
        self.launch = launch
        self.defective = defective
        self.allowed = False
        self.denied = False

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        decision = await self.policy.decide(request, layers, protected_paths)
        if decision.allow:
            await self.launch.command("allowed")
            self.allowed = True
        else:
            self.denied = True
            if self.defective:
                await self.launch.command("denied")
        return decision


class LaunchingHarness(ConfiguredHarness):
    def __init__(self, owner: ContextOwner) -> None:
        super().__init__()
        self.owner = owner
        self.workspace: Path | None = None
        self.started = asyncio.Event()

    def build_session_spec(
        self,
        config: JsonObject,
        workspace: WorkspaceTarget,
        feedback: None,
        *,
        inputs: SessionInputs,
    ) -> SessionSpec:
        self.workspace = Path(workspace.root)
        assert self.workspace.is_dir()
        if self.owner.mode == "spec_failure":
            raise RuntimeError("spec failed")
        return super().build_session_spec(
            {"prefix": "", **config}, workspace, feedback, inputs=inputs
        )

    async def run(
        self,
        spec: SessionSpec,
        policy: PolicyClient,
        sink: EventSink,
        launches: LaunchContext,
    ) -> SessionResult:
        current = self.owner.active
        assert current is not None
        assert self.workspace == current.workspace
        assert self.workspace is not None
        inputs = LaunchInputs({"PATH": os.defpath}, self.workspace)
        launch = await launches.allocate(inputs)
        assert current.launch is not None
        assert current.launch is launch
        await current.launch.start(inputs, launches.placements)
        self.started.set()
        await self.owner.report_started(current)
        if self.owner.mode == "failure":
            raise RuntimeError("adapter failed")
        if self.owner.mode == "cancel":
            await asyncio.get_running_loop().create_future()
        enforcing = ExecutingPolicy(
            policy, current.launch, self.owner.mode == "denied_execution"
        )
        result = await super().run(spec, enforcing, sink, launches)
        if enforcing.denied:
            sink.emit(
                "tool_result",
                {"tool_use_id": "two", "is_error": True, "truncated": False},
            )
        assert not (self.workspace / "denied").exists(), "denied operation executed"
        if enforcing.allowed:
            assert (self.workspace / "allowed").read_text() == "executed"
        if self.owner.mode == "kill":
            await launch.kill()
        else:
            await current.launch.command(
                "nonzero" if self.owner.mode == "nonzero" else "exit"
            )
        ending = await launch.ended()
        assert ending == await launch.ended()
        assert current.launch.process is not None
        if self.owner.mode == "kill":
            assert ending.signal == 9
            assert current.launch.process.returncode == -9
        else:
            assert ending.exit_code == (7 if self.owner.mode == "nonzero" else 0)
            assert ending.exit_code == current.launch.process.returncode
        return result


class Exercise:
    denied_tool = "Forbidden"

    def __init__(self, owner: ContextOwner) -> None:
        self.owner = owner
        self.context_factory = owner.context
        self.inputs = HarnessConformanceInputs(
            self.configured_failure,
            self.prepare_case,
            self.denied_tool,
            self.effect,
            self.context_factory,
        )

    def adapter(self, case: HarnessCase, ctx: ProtocolContext) -> HarnessAdapter:
        provider = LaunchingHarness(self.owner)
        provider.select(case, ctx)
        return provider

    async def prepare_case(
        self,
        provider: HarnessAdapter,
        config: JsonObject,
        case: HarnessCase,
        ctx: ProtocolContext,
    ) -> None:
        assert isinstance(provider, LaunchingHarness)
        assert provider.owner is self.owner
        assert self.owner.active is not None
        provider.select(case, ctx)
        if self.owner.mode == "prepare_cancel":
            await asyncio.get_running_loop().create_future()
        if self.owner.mode == "prepare_failure":
            raise RuntimeError("prepare failed")
        config["prefix"] = "mutated-preparation"

    async def failure(self, ctx: ProtocolContext, marker: str) -> object:
        return await self.configured_failure(
            LaunchingHarness(self.owner), {"prefix": "direct"}, ctx, marker
        )

    async def configured_failure(
        self,
        provider: HarnessAdapter,
        config: JsonObject,
        ctx: ProtocolContext,
        marker: str,
    ) -> object:
        assert isinstance(provider, LaunchingHarness)
        async with self.context_factory(HarnessCase.NO_USAGE, ctx) as supplied:
            provider.select(None, ctx)
            provider.script_source.script = ListedScript(
                ScriptHeader(end="completed", on_interrupt="acknowledge"),
                (
                    STEP_ADAPTER.validate_python(
                        {
                            "kind": "event",
                            "event": "turn",
                            "data": {
                                "message_id": "failure",
                                "truncated": False,
                                "text": marker,
                            },
                        }
                    ),
                ),
            )
            return await HarnessHost(supplied).hosted(
                provider, self.denied_tool, None, config
            )

    async def effect(
        self, provider: HarnessAdapter, config: JsonObject, ctx: ProtocolContext
    ) -> None:
        run = await run_harness_case(
            HarnessHost(), provider, config, ctx, self.inputs, HarnessCase.NO_USAGE
        )
        assert any(
            event.kind == "turn"
            and event.data.message_id == str(config["prefix"]) + "m1"
            for event in run.events
        )


class Adapter:
    def __init__(self, owner: ContextOwner | None = None) -> None:
        self.owner = ContextOwner() if owner is None else owner
        self.exercise = Exercise(self.owner)
        self.exercises = self.exercise.inputs

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        self.owner.lifetime = lifetime
        mode = fixture_data.get("mode", "success")
        assert isinstance(mode, str)
        self.owner.mode = mode
        feedback = fixture_data.get("feedback")
        assert feedback is None or isinstance(feedback, str)
        self.owner.feedback = feedback
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        return ConstructorArguments((self.owner,))

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None or isinstance(instance, LaunchingHarness)
        assert self.owner.active is None
        assert all(case.closed and not case.root.exists() for case in self.owner.cases)


def launching() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.HARNESS,
        "launching",
        LaunchingHarness,
        LaunchingHarness.config_schema,
        "1",
    )
