import asyncio
from dataclasses import replace
from functools import partial

import pytest

from kora.conformance import policy as checks
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
from kora.ir import ToolPolicy
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.policy import Policy, PolicyDecision, PolicyLayers, ToolCallRequest
from tests.conformance.policies import DECIDE_FAILED, ReferencePolicy, reference_calls
from tests.conformance.test_shared_secrets_checks import assert_capture_released

MARKER: str = "policy-shared-input-marker"


class LoopBoundPolicy(ReferencePolicy):
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[asyncio.Future[None] | None] = asyncio.Queue()
        self.worker = asyncio.create_task(self.serve())
        self.contexts: list[ProtocolContext] = []
        self.requests: list[ToolCallRequest] = []
        self.cleanup_loop: asyncio.AbstractEventLoop | None = None

    async def serve(self) -> None:
        while (completion := await self.queue.get()) is not None:
            if not completion.done():
                completion.set_result(None)

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        assert asyncio.get_running_loop() is self.loop
        completion: asyncio.Future[None] = self.loop.create_future()
        self.queue.put_nowait(completion)
        await completion
        self.contexts.append(ctx)
        self.requests.append(request)
        if request.tool == "ConformanceFailure":
            ctx.logger.emit(DECIDE_FAILED, reason="unreachable")
            raise KoraProtocolError("policy_failed", "the policy failed", False, {})
        return await super().decide(request, layers, protected_paths, ctx)

    async def close(self) -> None:
        self.queue.put_nowait(None)
        await self.worker
        self.cleanup_loop = asyncio.get_running_loop()


async def failing_input(
    provider: Policy, configuration: JsonObject, context: ProtocolContext, marker: str
) -> object:
    request = replace(
        reference_calls().shell,
        tool="ConformanceFailure",
        arguments={"input": marker, "configuration": configuration["deny"]},
    )
    empty = checks.policy()
    return await provider.decide(request, PolicyLayers(empty, empty), (), context)


async def configuration_effect(
    provider: Policy, configuration: JsonObject, context: ProtocolContext
) -> None:
    assert provider.validate(configuration) == []
    configured = ToolPolicy.model_validate(configuration)

    decision = await provider.decide(
        reference_calls().shell,
        PolicyLayers(configured, checks.policy()),
        (),
        context,
    )

    assert not decision.allow


async def exercise_policy(captures: list[Capture]) -> LoopBoundPolicy:
    provider = LoopBoundPolicy()
    configuration: JsonObject = {"deny": ["Bash(git)"]}
    inputs = checks.PolicyConformanceInputs(
        failing_input, reference_calls(), configuration_effect
    )
    try:
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "policy-shared", utc_now
        ) as capture:
            captures.append(capture)
            context = replace(context_for(capture), namespace_id="policy-shared")
            failure = partial(inputs.failure, provider, configuration)
            check_capabilities_are_frozen_and_consistent(provider)
            check_config_schema_is_an_object_schema(provider)
            checks.check_policy_validate_refuses_a_malformed_policy(
                provider, inputs.calls
            )
            for layer in checks.LAYERS:
                await checks.check_policy_deny_in_either_layer_denies(
                    provider, inputs.calls, context, layer
                )
                await checks.check_policy_allow_list_restricts_in_either_layer(
                    provider, inputs.calls, context, layer
                )
            await checks.check_policy_allows_what_no_rule_refuses(
                provider, inputs.calls, context
            )
            await checks.check_policy_protected_path_is_denied(
                provider, inputs.calls, context
            )
            await checks.check_policy_decisions_are_deterministic(
                provider, inputs.calls, context
            )
            await inputs.configuration_effect(provider, configuration, context)
            await check_failure_is_a_bounded_protocol_error(failure, context, MARKER)
            await check_provider_logs_through_its_context(
                failure, capture, context, MARKER
            )
            assert all(item is context for item in provider.contexts)
            assert [item.arguments["input"] for item in provider.requests[-2:]] == [
                MARKER
            ] * 2
    finally:
        await provider.close()
    return provider


def test_shared_policy_checks_use_one_caller_loop_and_exact_inputs() -> None:
    captures: list[Capture] = []
    with asyncio.Runner() as runner:
        expected_loop = runner.get_loop()

        provider = runner.run(exercise_policy(captures))

        assert provider.loop is provider.cleanup_loop is expected_loop
        assert provider.worker.done()
        assert provider.queue.empty()
        assert len(provider.requests) > 6000
        assert not asyncio.all_tasks(expected_loop)
    assert len(captures) == 1
    assert_capture_released(captures[0])


class FinalizingPolicy(ReferencePolicy):
    def __init__(self) -> None:
        self.pending: asyncio.Task[None] | None = None
        self.finished = False

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        decision = await super().decide(request, layers, protected_paths, ctx)
        if self.pending is None:
            started = asyncio.Event()

            async def finish_later() -> None:
                started.set()
                try:
                    await asyncio.Future[None]()
                finally:
                    ctx.logger.emit(DECIDE_FAILED, reason="finalized")
                    object.__setattr__(decision, "allow", True)
                    object.__setattr__(decision, "reason", "allowed")
                    self.finished = True

            self.pending = asyncio.create_task(finish_later())
            await started.wait()
        return decision


def test_sync_policy_check_observes_owned_loop_finalization() -> None:
    provider = FinalizingPolicy()
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "policy-finalization", utc_now
    ) as capture:
        with pytest.raises(pytest.fail.Exception, match="a matching deny denies"):
            checks.test_policy_deny_in_either_layer_denies(
                provider, reference_calls(), capture, "namespace"
            )

        assert provider.finished
        assert provider.pending is not None
        assert provider.pending.done()
        capture.finish()
        assert any(
            record.get("reason") == "finalized" for record in capture.reader.records()
        )
    assert_capture_released(capture)
