import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from functools import partial

import pytest

from kora.conformance import harness as checks
from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    check_capabilities_are_frozen_and_consistent,
    check_config_schema_is_an_object_schema,
    check_failure_is_a_bounded_protocol_error,
    check_provider_logs_through_its_context,
    context_for,
    utc_now,
)
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.plugins.harness.fake import FakeHarness, ListedScript
from kora.plugins.harness.fake.script import EventStep, Step
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EventSink,
    HarnessAdapter,
    LaunchContext,
    SessionResult,
    SessionSpec,
)
from kora.protocols.policy import PolicyClient
from kora.protocols.vcs import WorkspaceTarget
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.plugins.harness.fake.fake_support import (
    CASES,
    DENIED_TOOL,
    scope,
    scripted,
    turn,
)

MARKER: str = "harness-shared-input-marker"
CONFIGURATION: JsonObject = {"session": {"prefix": "configured"}}


class LoopBoundScript:
    def __init__(self, phase: str = "run", ending: str = "success") -> None:
        self.loop = asyncio.get_running_loop()
        self.script = CASES[checks.HarnessCase.TOOL_CALLS]
        self.header = self.script.header
        self.queue: asyncio.Queue[asyncio.Future[None] | None] = asyncio.Queue()
        self.worker = asyncio.create_task(self.serve())
        self.entered = asyncio.Event()
        self.phase = phase
        self.ending = ending
        self.prefix = ""
        self.active = 0
        self.prepared = 0
        self.started = 0
        self.finished = 0
        self.cleanup_loop: asyncio.AbstractEventLoop | None = None

    async def serve(self) -> None:
        while (completion := await self.queue.get()) is not None:
            if not completion.done():
                completion.set_result(None)

    async def advance(self, phase: str) -> None:
        assert asyncio.get_running_loop() is self.loop
        completion: asyncio.Future[None] = self.loop.create_future()
        self.queue.put_nowait(completion)
        await completion
        if phase == self.phase:
            self.entered.set()
            if self.ending == "failure":
                raise KoraProtocolError(
                    "exercise_failed", "the exercise failed", False, {}
                )
            if self.ending == "unexpected":
                raise RuntimeError("unexpected exercise failure")
            if self.ending == "cancel":
                await self.loop.create_future()

    async def select(self, script: ListedScript) -> None:
        await self.advance("prepare")
        self.script = script
        self.header = script.header
        self.prepared += 1

    async def steps(self) -> AsyncGenerator[Step]:
        self.started += 1
        self.active += 1
        try:
            await self.advance("run")
            async with aclosing(self.script.steps()) as steps:
                async for step in steps:
                    if isinstance(step, EventStep) and step.event == "turn":
                        data = {
                            **step.data,
                            "message_id": self.prefix + str(step.data["message_id"]),
                        }
                        yield step.model_copy(update={"data": data})
                    else:
                        yield step
        finally:
            self.active -= 1
            self.finished += 1

    async def close(self) -> None:
        self.queue.put_nowait(None)
        await self.worker
        self.cleanup_loop = asyncio.get_running_loop()


class ConfiguredHarness(FakeHarness):
    config_schema: JsonObject = {"type": "object"}

    def __init__(self, source: LoopBoundScript, context: ProtocolContext) -> None:
        super().__init__(source, scope(), context)
        self.script_source = source
        self.configurations: list[JsonObject] = []

    def build_session_spec(
        self, config: JsonObject, workspace: WorkspaceTarget, feedback: None
    ) -> SessionSpec:
        self.configurations.append(deepcopy(config))
        return (
            super()
            .build_session_spec(config, workspace, feedback)
            .model_copy(update={"options": deepcopy(config)})
        )

    async def run(
        self,
        spec: SessionSpec,
        policy: PolicyClient,
        sink: EventSink,
        launches: LaunchContext,
    ) -> SessionResult:
        session = spec.options.get("session", {})
        assert isinstance(session, dict)
        prefix = session.get("prefix", "")
        assert isinstance(prefix, str)
        self.script_source.prefix = prefix
        return await super().run(spec, policy, sink, launches)


async def prepare_case(
    provider: HarnessAdapter,
    configuration: JsonObject,
    case: checks.HarnessCase,
    context: ProtocolContext,
) -> None:
    assert isinstance(provider, ConfiguredHarness)
    assert provider.ctx is context
    session = configuration.get("session")
    if isinstance(session, dict):
        session["prefix"] = "preparation-must-not-change-the-session"
    await provider.script_source.select(CASES[case])


async def failing_input(
    provider: HarnessAdapter,
    configuration: JsonObject,
    context: ProtocolContext,
    marker: str,
) -> object:
    assert isinstance(provider, ConfiguredHarness)
    assert provider.ctx is context
    await provider.script_source.select(scripted([turn("failure", marker)]))
    return await checks.HarnessHost().hosted(provider, DENIED_TOOL, None, configuration)


async def configuration_effect(
    provider: HarnessAdapter, configuration: JsonObject, context: ProtocolContext
) -> None:
    run = await checks.run_harness_case(
        checks.HarnessHost(),
        provider,
        configuration,
        context,
        INPUTS,
        checks.HarnessCase.NO_USAGE,
    )

    assert any(
        event.kind == "turn" and event.data.message_id == "configuredm1"
        for event in run.events
    )


INPUTS: checks.HarnessConformanceInputs = checks.HarnessConformanceInputs(
    failing_input, prepare_case, DENIED_TOOL, configuration_effect
)


async def exercise_harness(captures: list[Capture]) -> ConfiguredHarness:
    source = LoopBoundScript()
    try:
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "harness-shared", utc_now
        ) as capture:
            captures.append(capture)
            context = replace(context_for(capture), namespace_id="harness-shared")
            provider = ConfiguredHarness(source, context)
            host = checks.HarnessHost()
            runs: dict[checks.HarnessCase, checks.HostedRun] = {}
            check_capabilities_are_frozen_and_consistent(provider)
            check_config_schema_is_an_object_schema(provider)
            for case in checks.HarnessCase:
                runs[case] = await checks.run_harness_case(
                    host,
                    provider,
                    CONFIGURATION,
                    context,
                    INPUTS,
                    case,
                    checks.MAX_EVENTS
                    if case is checks.HarnessCase.MAX_EVENTS
                    else None,
                )
            checks.check_harness_events_are_normalised(
                runs[checks.HarnessCase.TOOL_CALLS]
            )
            checks.check_harness_policy_precedes_every_tool_call(
                runs[checks.HarnessCase.TOOL_CALLS]
            )
            checks.check_harness_max_events_denies_at_exact_count(
                runs[checks.HarnessCase.MAX_EVENTS]
            )
            checks.check_harness_usage_completeness(
                runs[checks.HarnessCase.USAGE], runs[checks.HarnessCase.NO_USAGE]
            )
            checks.check_harness_structured_output_and_fallbacks(
                runs[checks.HarnessCase.STRUCTURED_OUTPUT],
                runs[checks.HarnessCase.RESULT_TEXT],
            )
            await INPUTS.configuration_effect(provider, CONFIGURATION, context)
            empty = await checks.run_harness_case(
                host, provider, {}, context, INPUTS, checks.HarnessCase.NO_USAGE
            )
            assert any(
                event.kind == "turn" and event.data.message_id == "m1"
                for event in empty.events
            )
            failure = partial(INPUTS.failure, provider, CONFIGURATION)
            await check_failure_is_a_bounded_protocol_error(failure, context, MARKER)
            await check_provider_logs_through_its_context(
                failure, capture, context, MARKER
            )
    finally:
        await source.close()
    return provider


def test_shared_harness_checks_keep_exact_provider_and_configuration() -> None:
    captures: list[Capture] = []
    with asyncio.Runner() as runner:
        expected_loop = runner.get_loop()

        provider = runner.run(exercise_harness(captures))

        assert not asyncio.all_tasks(expected_loop)
        assert (
            provider.script_source.loop
            is provider.script_source.cleanup_loop
            is expected_loop
        )
    assert provider.configurations == [CONFIGURATION] * 7 + [{}] + [CONFIGURATION] * 2
    assert CONFIGURATION == {"session": {"prefix": "configured"}}
    assert provider.script_source.started == provider.script_source.finished == 10
    assert provider.script_source.active == 0
    failing_step = provider.script_source.script.listed[0]
    assert isinstance(failing_step, EventStep)
    assert failing_step.data["text"] == MARKER
    assert provider.script_source.worker.done()
    assert provider.script_source.queue.empty()
    assert_capture_released(captures[0])


async def exercise_ending(
    phase: str, ending: str, captures: list[Capture], sources: list[LoopBoundScript]
) -> None:
    source = LoopBoundScript(phase, ending)
    sources.append(source)
    try:
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "harness-ending", utc_now
        ) as capture:
            captures.append(capture)
            context = context_for(capture)
            provider = ConfiguredHarness(source, context)
            async with asyncio.timeout(5):
                operation = asyncio.create_task(
                    checks.run_harness_case(
                        checks.HarnessHost(),
                        provider,
                        CONFIGURATION,
                        context,
                        INPUTS,
                        checks.HarnessCase.TOOL_CALLS,
                    )
                )
                try:
                    if ending == "cancel":
                        await source.entered.wait()
                        operation.cancel()
                    await operation
                finally:
                    if not operation.done():
                        operation.cancel()
                        await asyncio.gather(operation, return_exceptions=True)
    finally:
        await source.close()


@pytest.mark.parametrize("phase", ["prepare", "run"])
@pytest.mark.parametrize("ending", ["success", "failure", "cancel", "unexpected"])
def test_shared_harness_checks_release_every_ending(phase: str, ending: str) -> None:
    captures: list[Capture] = []
    sources: list[LoopBoundScript] = []
    with asyncio.Runner() as runner:
        operation = exercise_ending(phase, ending, captures, sources)

        if ending == "failure":
            with pytest.raises(KoraProtocolError, match="the exercise failed"):
                runner.run(operation)
        elif ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                runner.run(operation)
        elif ending == "unexpected":
            with pytest.raises(RuntimeError, match="unexpected exercise failure"):
                runner.run(operation)
        else:
            runner.run(operation)

        assert not asyncio.all_tasks(runner.get_loop())
    assert len(captures) == len(sources) == 1
    source = sources[0]
    assert source.started == source.finished
    assert source.active == 0
    assert source.worker.done()
    assert source.queue.empty()
    assert source.loop is source.cleanup_loop
    if phase == "prepare" and ending != "success":
        assert source.started == 0
    assert_capture_released(captures[0])
