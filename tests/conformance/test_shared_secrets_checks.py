import asyncio
from dataclasses import replace
from functools import partial

import pytest

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
from kora.conformance.secrets import (
    SecretsConformanceInputs,
    check_secrets_forbidden_reference_is_refused_like_a_missing_one,
    check_secrets_list_names_returns_names_only,
    check_secrets_missing_reference_is_a_bounded_error,
    check_secrets_resolution_returns_a_masked_value,
    check_secrets_value_is_masked_in_a_captured_stream,
)
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings, current_diagnostics
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretValue
from kora.protocols.secrets import SecretsProvider
from tests.conformance.providers import (
    FORBIDDEN,
    MISSING,
    REFERENCE,
    VALUE,
    ReferenceSecretsProvider,
)

SUPPLIED_MARKER: str = "shared-check-supplied-marker"


class LoopBoundSecretsProvider(ReferenceSecretsProvider):
    def __init__(self, *, echo: bool = False) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[asyncio.Future[None] | None] = asyncio.Queue()
        self.worker = asyncio.create_task(self.serve())
        self.contexts: list[ProtocolContext] = []
        self.references: list[str] = []
        self.operation_loops: list[asyncio.AbstractEventLoop] = []
        self.cleanup_loop: asyncio.AbstractEventLoop | None = None
        self.echo = echo

    async def serve(self) -> None:
        while (completion := await self.queue.get()) is not None:
            if not completion.done():
                completion.set_result(None)

    async def round_trip(self) -> None:
        completion: asyncio.Future[None] = self.loop.create_future()
        self.queue.put_nowait(completion)
        await completion
        self.operation_loops.append(asyncio.get_running_loop())

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        await self.round_trip()
        self.contexts.append(ctx)
        self.references.append(reference)
        if self.echo:
            raise KoraProtocolError("secret_not_found", reference, False, {})
        return await super().resolve(reference, ctx)

    async def list_names(self, prefix: str) -> list[str]:
        await self.round_trip()
        return await super().list_names(prefix)

    async def close(self) -> None:
        self.queue.put_nowait(None)
        await self.worker
        self.cleanup_loop = asyncio.get_running_loop()


async def failing_input(
    provider: SecretsProvider,
    configuration: JsonObject,
    context: ProtocolContext,
    marker: str,
) -> object:
    return await provider.resolve(f"{configuration['missing']}/{marker}", context)


async def configuration_effect(
    provider: SecretsProvider, configuration: JsonObject, context: ProtocolContext
) -> None:
    reference = configuration["reference"]
    assert isinstance(reference, str)

    value = await provider.resolve(reference, context)

    assert value.reveal() == configuration["expected"]


def supplied_context(capture: Capture) -> ProtocolContext:
    return replace(context_for(capture), namespace_id="shared-check-namespace")


def assert_capture_released(capture: Capture) -> None:
    assert capture.finished
    assert capture.pipe.read_end == -1
    assert capture.pipe.write_end == -1
    assert not capture.diagnostics.writer_is_alive()
    assert current_diagnostics() is None


async def exercise_shared_checks(captures: list[Capture]) -> LoopBoundSecretsProvider:
    provider = LoopBoundSecretsProvider()
    inputs = SecretsConformanceInputs(
        failure=failing_input,
        resolvable=(REFERENCE, VALUE),
        missing_reference=MISSING,
        forbidden_reference=FORBIDDEN,
        listed_prefix="",
        configuration_effect=configuration_effect,
    )
    configuration: JsonObject = {
        "missing": MISSING,
        "reference": REFERENCE,
        "expected": VALUE,
    }
    failure = partial(inputs.failure, provider, configuration)
    contexts: list[ProtocolContext] = []
    try:
        check_capabilities_are_frozen_and_consistent(provider)
        check_config_schema_is_an_object_schema(provider)
        for case in ("failure", "logging", "resolve", "missing", "forbidden", "stream"):
            with captured(
                CONFORMANCE_COMPONENT, LoggingSettings(), "shared-check", utc_now
            ) as capture:
                captures.append(capture)
                context = supplied_context(capture)
                contexts.append(context)
                if case == "failure":
                    await check_failure_is_a_bounded_protocol_error(
                        failure, context, SUPPLIED_MARKER
                    )
                elif case == "logging":
                    await check_provider_logs_through_its_context(
                        failure, capture, context, SUPPLIED_MARKER
                    )
                elif case == "resolve":
                    await check_secrets_resolution_returns_a_masked_value(
                        provider, inputs.resolvable, capture, context
                    )
                elif case == "missing":
                    await check_secrets_missing_reference_is_a_bounded_error(
                        provider, inputs.missing_reference, context
                    )
                elif case == "forbidden":
                    await (
                        check_secrets_forbidden_reference_is_refused_like_a_missing_one(
                            provider, inputs.forbidden_reference, context
                        )
                    )
                else:
                    await check_secrets_value_is_masked_in_a_captured_stream(
                        provider, inputs.resolvable, capture, context
                    )
            assert_capture_released(capture)
        await check_secrets_list_names_returns_names_only(
            provider, inputs.listed_prefix, inputs.resolvable
        )
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "configuration", utc_now
        ) as capture:
            captures.append(capture)
            context = supplied_context(capture)
            contexts.append(context)
            await inputs.configuration_effect(provider, configuration, context)
    finally:
        await provider.close()

    assert all(
        actual is expected
        for actual, expected in zip(provider.contexts, contexts, strict=True)
    )
    assert provider.references[:2] == [f"{MISSING}/{SUPPLIED_MARKER}"] * 2
    return provider


def test_shared_checks_use_one_caller_loop_and_exact_inputs() -> None:
    captures: list[Capture] = []
    with asyncio.Runner() as runner:
        expected_loop = runner.get_loop()

        provider = runner.run(exercise_shared_checks(captures))

        assert provider.loop is expected_loop
        assert provider.cleanup_loop is expected_loop
        assert provider.operation_loops == [expected_loop] * 8
        assert provider.worker.done()
        assert provider.queue.empty()
        assert len(asyncio.all_tasks(expected_loop)) == 0
    assert len(captures) == 7
    for capture in captures:
        assert_capture_released(capture)


async def exercise_ending(
    ending: str, captures: list[Capture], providers: list[LoopBoundSecretsProvider]
) -> None:
    provider = LoopBoundSecretsProvider(echo=ending == "failure")
    providers.append(provider)
    try:
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "shared-ending", utc_now
        ) as capture:
            captures.append(capture)
            context = supplied_context(capture)
            configuration: JsonObject = (
                {} if ending == "unexpected" else {"missing": MISSING}
            )
            failure = partial(failing_input, provider, configuration)
            async with asyncio.timeout(0 if ending == "cancel" else 5):
                await check_failure_is_a_bounded_protocol_error(
                    failure, context, SUPPLIED_MARKER
                )
    finally:
        await provider.close()


@pytest.mark.parametrize("ending", ["success", "failure", "cancel", "unexpected"])
def test_shared_checks_release_capture_on_every_ending(ending: str) -> None:
    captures: list[Capture] = []
    providers: list[LoopBoundSecretsProvider] = []
    with asyncio.Runner() as runner:
        operation = exercise_ending(ending, captures, providers)

        if ending == "failure":
            with pytest.raises(
                pytest.fail.Exception, match="the message carries no input"
            ):
                runner.run(operation)
        elif ending == "cancel":
            with pytest.raises(TimeoutError):
                runner.run(operation)
        elif ending == "unexpected":
            with pytest.raises(KeyError, match="missing"):
                runner.run(operation)
        else:
            runner.run(operation)

        assert len(asyncio.all_tasks(runner.get_loop())) == 0
    assert len(providers) == len(captures) == 1
    assert providers[0].worker.done()
    assert providers[0].cleanup_loop is providers[0].loop
    assert_capture_released(captures[0])
