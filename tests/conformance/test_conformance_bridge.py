import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from kora.conformance.bridge import (
    ConformanceBridgeError,
    ConformanceSelection,
    ConstructorArguments,
    PreparedConformance,
    ProtocolExerciseBundle,
)
from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    MARKER,
    check_capabilities_are_frozen_and_consistent,
    check_config_schema_is_an_object_schema,
    check_failure_is_a_bounded_protocol_error,
    check_provider_logs_through_its_context,
    context_for,
    utc_now,
)
from kora.conformance.inputs import ConformanceInputError
from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.conformance.secrets import (
    SecretsConformanceInputs,
    check_secrets_list_names_returns_names_only,
    check_secrets_missing_reference_is_a_bounded_error,
    check_secrets_resolution_returns_a_masked_value,
    check_secrets_value_is_masked_in_a_captured_stream,
)
from kora.conformance.secrets import (
    check_secrets_forbidden_reference_is_refused_like_a_missing_one as check_forbidden,
)
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import (
    ConformanceLifetime,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.secret_value import SecretValue
from kora.protocols.secrets import SecretsCapabilities, SecretsProvider
from tests.conformance.providers import FORBIDDEN, MISSING, REFERENCE
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits
from tests.conformance.test_shared_secrets_checks import (
    LoopBoundSecretsProvider,
    assert_capture_released,
)


def schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }


class ConfiguredSecrets(LoopBoundSecretsProvider):
    def __init__(self, value: str, phase: str) -> None:
        super().__init__()
        self.value = value
        self.config_schema = schema()
        if phase == "schema":
            self.config_schema["title"] = "different"

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        if reference != REFERENCE or ctx.secret_admission is None:
            return await super().resolve(reference, ctx)
        await self.round_trip()
        self.contexts.append(ctx)
        self.references.append(reference)
        return ctx.secret_admission.admit(self.value)


class WrappedSecrets:
    name: str = "wrapped"

    def __init__(self, provider: ConfiguredSecrets) -> None:
        self.provider = provider

    @property
    def config_schema(self) -> JsonObject:
        return self.provider.config_schema

    def capabilities(self) -> SecretsCapabilities:
        return self.provider.capabilities()

    async def resolve(self, reference: str, ctx: ProtocolContext) -> SecretValue:
        return await self.provider.resolve(reference, ctx)

    async def list_names(self, prefix: str) -> list[str]:
        return await self.provider.list_names(prefix)


async def failure(
    provider: SecretsProvider,
    configuration: JsonObject,
    context: ProtocolContext,
    marker: str,
) -> object:
    assert isinstance(configuration["value"], str)
    return await provider.resolve(f"{MISSING}/{marker}", context)


async def effect(
    provider: SecretsProvider, configuration: JsonObject, context: ProtocolContext
) -> None:
    value = await provider.resolve(REFERENCE, context)
    assert value.reveal() == configuration["value"]


class Adapter:
    def __init__(
        self, value: str, phase: str = "success", wrapped: bool = False
    ) -> None:
        self.value = value
        self.phase = phase
        self.wrapped = wrapped
        self.preparations: list[JsonObject] = []
        self.bindings: list[JsonObject] = []
        self.created: list[ConfiguredSecrets] = []
        self.returned: list[object] = []
        self.cleaned: list[object | None] = []
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.authority: ConformanceLifetime | None = None
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.exercise_started = asyncio.Event()
        self.exercises: ProtocolExerciseBundle = SecretsConformanceInputs(
            failure, (REFERENCE, value), MISSING, FORBIDDEN, "", effect
        )
        if phase == "bundle":
            self.exercises = cast("ProtocolExerciseBundle", object())

    def construct(self, *, value: str) -> object:
        self.loops.append(asyncio.get_running_loop())
        if self.phase == "construct":
            raise ValueError("construct failed")
        provider = ConfiguredSecrets(value, self.phase)
        self.created.append(provider)
        returned = WrappedSecrets(provider) if self.wrapped else provider
        self.returned.append(returned)
        return returned

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        self.loops.append(asyncio.get_running_loop())
        self.authority = lifetime
        self.preparations.append(dict(config))
        assert fixture_data == {"value": "conflicting-fixture-value"}
        config.clear()
        fixture_data.clear()
        if self.phase == "prepare":
            reserved = await lifetime.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            acquired = await lifetime.acquire(
                reserved.token, new_operation_id(), TemporaryTreeRequest()
            )
            await lifetime.acknowledge(
                reserved.token, acquired.operation_id, acquired.revision
            )
            raise ValueError("prepare failed")
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        self.loops.append(asyncio.get_running_loop())
        self.bindings.append(dict(config))
        if self.phase == "bind":
            raise ValueError("bind failed")
        if self.phase == "arguments":
            return cast("ConstructorArguments", object())
        arguments = ConstructorArguments(kwargs=config)
        config.clear()
        return arguments

    async def cleanup(self, instance: object | None) -> None:
        self.loops.append(asyncio.get_running_loop())
        self.cleaned.append(instance)
        self.cleanup_started.set()
        if self.phase in {"cancel", "cancel-cleanup"}:
            await self.cleanup_release.wait()
        for provider in self.created:
            await provider.close()
        if self.phase in {"cleanup", "exercise-cleanup", "cancel-cleanup"}:
            raise RuntimeError("cleanup failed")
        if self.phase == "cleanup-cancelled":
            raise asyncio.CancelledError


def selected(adapter: Adapter) -> tuple[ConformanceSelection, ProviderMetadata]:
    metadata = ProviderMetadata(
        ProtocolName.SECRETS, "configured", adapter.construct, schema(), "1"
    )
    selection = ConformanceSelection(
        metadata,
        json.dumps({"value": adapter.value}).encode(),
        b'{"value":"conflicting-fixture-value"}',
    )
    return selection, metadata


@asynccontextmanager
async def owned_lifetime(root: Path) -> AsyncGenerator[ParentLifetime]:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        limits=limits(),
        launcher=InstalledIOHelperLauncher(home=root, state_home=root / "state"),
    )
    try:
        yield owner
    finally:
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "wrapped"), [("first-value", False), ("second-value", True)]
)
async def test_bridge_preserves_selected_object_configuration_and_loop(
    tmp_path: Path, value: str, wrapped: bool
) -> None:
    adapter = Adapter(value, wrapped=wrapped)
    selection, metadata = selected(adapter)
    metadata.config_schema.clear()
    captures: list[Capture] = []

    async with owned_lifetime(tmp_path) as lifetime:
        async with selection.prepare(adapter, lifetime) as invocation:
            provider = cast("SecretsProvider", invocation.instance)
            inputs = invocation.exercises
            assert isinstance(inputs, SecretsConformanceInputs)
            assert provider is adapter.returned[0]
            check_capabilities_are_frozen_and_consistent(provider)
            check_config_schema_is_an_object_schema(provider)
            for case in (
                "failure",
                "logging",
                "resolve",
                "missing",
                "forbidden",
                "names",
                "stream",
            ):
                with captured(
                    CONFORMANCE_COMPONENT, LoggingSettings(), "bridge", utc_now
                ) as capture:
                    captures.append(capture)
                    context = context_for(capture)
                    authoritative = invocation.configuration
                    invocation.configuration.clear()
                    await inputs.configuration_effect(provider, authoritative, context)

                    async def exercise(ctx: ProtocolContext, marker: str) -> object:
                        return await inputs.failure(
                            provider, invocation.configuration, ctx, marker
                        )

                    if case == "failure":
                        await check_failure_is_a_bounded_protocol_error(
                            exercise, context, MARKER
                        )
                    elif case == "logging":
                        await check_provider_logs_through_its_context(
                            exercise, capture, context, MARKER
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
                        await check_forbidden(
                            provider, inputs.forbidden_reference, context
                        )
                    elif case == "names":
                        await check_secrets_list_names_returns_names_only(
                            provider, inputs.listed_prefix, inputs.resolvable
                        )
                    else:
                        await check_secrets_value_is_masked_in_a_captured_stream(
                            provider, inputs.resolvable, capture, context
                        )

    assert adapter.preparations == adapter.bindings == [{"value": value}]
    assert adapter.cleaned == adapter.returned
    assert adapter.authority is lifetime
    assert all(loop is asyncio.get_running_loop() for loop in adapter.loops)
    assert adapter.created[0].worker.done()
    assert adapter.created[0].cleanup_loop is asyncio.get_running_loop()
    for capture in captures:
        assert_capture_released(capture)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "prepare",
        "bind",
        "arguments",
        "bundle",
        "construct",
        "schema",
        "exercise",
        "cleanup",
        "exercise-cleanup",
        "cleanup-cancelled",
    ],
)
async def test_bridge_cleanup_retains_exact_primary_and_original_failure(
    tmp_path: Path, phase: str
) -> None:
    adapter = Adapter("value", phase)
    selection, _ = selected(adapter)
    expected = (
        asyncio.CancelledError
        if phase == "cleanup-cancelled"
        else (RuntimeError if phase == "cleanup" else ValueError)
    )

    async with owned_lifetime(tmp_path) as lifetime:

        async def exercise() -> None:
            async with selection.prepare(adapter, lifetime):
                if phase in {"exercise", "exercise-cleanup"}:
                    raise ValueError("exercise failed")

        with pytest.raises(expected) as raised:
            await exercise()

        assert adapter.cleaned == (
            []
            if phase == "prepare"
            else [adapter.returned[0] if adapter.returned else None]
        )
        if phase == "exercise-cleanup":
            assert str(raised.value) == "exercise failed"
            assert isinstance(raised.value.__cause__, RuntimeError)
        if phase == "prepare":
            assert lifetime.records
            assert any(
                record.directory is not None and not record.directory.closed
                for record in lifetime.records.values()
            )

    assert all(provider.worker.done() for provider in adapter.created)
    assert lifetime.inventory().retained_processes == 0
    assert lifetime.inventory().helpers.retained_helpers == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["cancel", "cancel-cleanup"])
async def test_bridge_repeated_cancellation_waits_for_real_cleanup(
    tmp_path: Path, phase: str
) -> None:
    adapter = Adapter("value", phase)
    selection, _ = selected(adapter)

    async with owned_lifetime(tmp_path) as lifetime:

        async def exercise() -> None:
            async with selection.prepare(adapter, lifetime):
                adapter.exercise_started.set()
                await asyncio.Future[None]()

        task = asyncio.create_task(exercise())
        try:
            await asyncio.wait_for(adapter.exercise_started.wait(), 5)
            task.cancel()
            await asyncio.wait_for(adapter.cleanup_started.wait(), 5)
            for _ in range(3):
                task.cancel()
                checkpoint: asyncio.Future[None] = (
                    asyncio.get_running_loop().create_future()
                )
                asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
                await checkpoint
                assert not task.done()
                assert not adapter.created[0].worker.done()
            adapter.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 5)
        finally:
            adapter.cleanup_release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert adapter.cleaned == adapter.returned
    assert adapter.created[0].worker.done()
    if phase == "cancel-cleanup":
        assert isinstance(raised.value.__cause__, RuntimeError)


class MissingBinding:
    def __init__(self) -> None:
        self.cleaned: list[object | None] = []

    async def cleanup(self, instance: object | None) -> None:
        self.cleaned.append(instance)


class MalformedAdapter(Adapter):
    def __init__(self, returned: object) -> None:
        super().__init__("value")
        self.malformed = returned

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        return cast("PreparedConformance", self.malformed)


@pytest.mark.asyncio
async def test_bridge_refuses_malformed_handoffs_without_losing_cleanup(
    tmp_path: Path,
) -> None:
    prepared = MissingBinding()
    adapter = MalformedAdapter(prepared)
    selection, _ = selected(adapter)

    async with owned_lifetime(tmp_path) as lifetime:
        for candidate in (object(), MalformedAdapter(object()), adapter):
            with pytest.raises(ConformanceBridgeError):
                async with selection.prepare(candidate, lifetime):
                    pytest.fail("malformed handoff reached exercises")

    assert prepared.cleaned == [None]
    assert adapter.created == []


def test_bridge_invalid_configuration_never_prepares_or_constructs() -> None:
    adapter = Adapter("value")
    _, metadata = selected(adapter)

    with pytest.raises(ConformanceInputError):
        ConformanceSelection(metadata, b'{"value":1}')

    assert adapter.preparations == []
    assert adapter.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failure", "cancel"])
async def test_bridge_cleanup_does_not_adopt_an_outer_handled_exception(
    tmp_path: Path, ending: str
) -> None:
    adapter = Adapter("value", "cleanup" if ending == "failure" else "cancel")
    selection, _ = selected(adapter)

    async with owned_lifetime(tmp_path) as lifetime:

        async def exercise() -> None:
            try:
                raise LookupError("already handled outside this invocation")
            except LookupError:
                async with selection.prepare(adapter, lifetime):
                    await adapter.created[0].round_trip()

        if ending == "failure":
            with pytest.raises(RuntimeError, match="cleanup failed"):
                await exercise()
        else:
            task = asyncio.create_task(exercise())
            try:
                await asyncio.wait_for(adapter.cleanup_started.wait(), 5)
                task.cancel()
                adapter.cleanup_release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            finally:
                adapter.cleanup_release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    assert adapter.cleaned == adapter.returned
    assert adapter.created[0].worker.done()
