import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeVar

import pytest

from kora.conformance import artifact_storage as checks
from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    check_failure_is_a_bounded_protocol_error,
    check_provider_logs_through_its_context,
    context_for,
    utc_now,
)
from kora.conformance.storage_support import operation, seal_bytes, stage_spec
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage import ArtifactStorage, StorageByteStream
from kora.protocols.storage_models import (
    ContentRef,
    PromotionRequest,
    PromotionResult,
    StageRef,
    StageRetirement,
    StorageLimits,
    StorageOperationRef,
    StoragePage,
    StorageScanCursor,
    StorageScanKind,
)
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.inspection import InspectedStorage

Result = TypeVar("Result")
StorageCheck = Callable[[ArtifactStorage, ProtocolContext], Awaitable[None]]
CASES: tuple[StorageCheck, ...] = (
    checks.check_artifact_storage_promotion_is_create_only,
    checks.check_artifact_storage_staging_lifetime,
    checks.check_artifact_storage_deletion_lifetime,
    checks.check_artifact_storage_inventory_is_bounded,
    checks.check_artifact_storage_range_reads_are_exact,
    checks.check_artifact_storage_range_lifetime,
)
MARKER: str = "shared-storage-input-marker"


class LoopBoundStorage(InspectedStorage):
    def __init__(self, configuration: LocalStorageConfiguration) -> None:
        super().__init__(configuration)
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[asyncio.Future[None] | None] = asyncio.Queue()
        self.worker = asyncio.create_task(self.serve())
        self.contexts: list[ProtocolContext] = []
        self.operations: list[str] = []
        self.cleanup_loop: asyncio.AbstractEventLoop | None = None

    async def serve(self) -> None:
        while (completion := await self.queue.get()) is not None:
            if not completion.done():
                completion.set_result(None)

    async def dispatch(
        self,
        operation: str,
        function: Callable[[], Awaitable[Result]],
        ctx: ProtocolContext,
        control: bool = False,
    ) -> Result:
        assert asyncio.get_running_loop() is self.loop
        completion: asyncio.Future[None] = self.loop.create_future()
        self.queue.put_nowait(completion)
        await completion
        self.contexts.append(ctx)
        self.operations.append(operation)
        return await super().dispatch(operation, function, ctx, control)

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            if not self.worker.done():
                self.queue.put_nowait(None)
            await self.worker
            self.cleanup_loop = asyncio.get_running_loop()

    def assert_closed(self) -> None:
        super().assert_closed()
        assert self.worker.done()
        assert not self.worker.cancelled()
        assert self.queue.empty()
        assert self.cleanup_loop is self.loop


class WrongRangeStorage(LoopBoundStorage):
    async def get_range(
        self, content: ContentRef, offset: int, length: int, ctx: ProtocolContext
    ) -> StorageByteStream:
        return await super().get_range(content, offset + 1, length, ctx)


class WrongPromotionStorage(LoopBoundStorage):
    async def promote(
        self, request: PromotionRequest, ctx: ProtocolContext
    ) -> PromotionResult:
        result = await super().promote(request, ctx)
        return result.model_copy(update={"created_object": False})


class WrongInventoryStorage(LoopBoundStorage):
    async def scan(
        self,
        kind: StorageScanKind,
        cursor: StorageScanCursor | None,
        limit: int,
        ctx: ProtocolContext,
    ) -> StoragePage:
        page = await super().scan(kind, cursor, limit, ctx)
        return page if cursor is None else page.model_copy(update={"entries": ()})


class WrongRetirementStorage(LoopBoundStorage):
    async def retire_stage(
        self, operation: StorageOperationRef, stage: StageRef, ctx: ProtocolContext
    ) -> StageRetirement:
        result = await super().retire_stage(operation, stage, ctx)
        return result.model_copy(update={"completed": True, "remaining_effects": 0})


class RefusingReaderStorage(LoopBoundStorage):
    async def get_range(
        self, content: ContentRef, offset: int, length: int, ctx: ProtocolContext
    ) -> StorageByteStream:
        await super().get_range(content, offset, length, ctx)
        raise KoraProtocolError("storage_unavailable", "reader refused", False, {})


class FailingReaderStorage(LoopBoundStorage):
    async def get_range(
        self, content: ContentRef, offset: int, length: int, ctx: ProtocolContext
    ) -> StorageByteStream:
        await super().get_range(content, offset, length, ctx)
        raise ValueError("reader handoff failed")


def configuration(root: Path) -> LocalStorageConfiguration:
    return LocalStorageConfiguration(
        root=str(root / "store"), limits=StorageLimits(version=1)
    )


async def exercise_case(
    root: Path, case: StorageCheck, capture: Capture
) -> LoopBoundStorage:
    provider = LoopBoundStorage(configuration(root))
    context = replace(context_for(capture), namespace_id="shared-storage-namespace")

    try:
        await case(provider, context)
    finally:
        await provider.aclose()

    assert provider.contexts
    assert all(received is context for received in provider.contexts)
    assert "begin_stage" in provider.operations
    if case in (
        checks.check_artifact_storage_promotion_is_create_only,
        checks.check_artifact_storage_deletion_lifetime,
        checks.check_artifact_storage_range_reads_are_exact,
        checks.check_artifact_storage_range_lifetime,
    ):
        assert "promote" in provider.operations
    provider.assert_closed()
    return provider


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.__name__)
def test_shared_storage_check_uses_caller_loop_and_exact_provider(
    case: StorageCheck,
) -> None:
    with TemporaryDirectory(prefix="kora-shared-storage-") as directory:
        root = Path(directory)
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "shared-storage", utc_now
        ) as capture:
            with asyncio.Runner() as runner:
                provider = runner.run(exercise_case(root, case, capture))

    assert not root.exists()
    provider.assert_closed()
    assert_capture_released(capture)


@pytest.mark.parametrize(
    ("provider_type", "case", "message"),
    [
        (
            WrongPromotionStorage,
            checks.check_artifact_storage_promotion_is_create_only,
            "first publication reports creation",
        ),
        (
            WrongRangeStorage,
            checks.check_artifact_storage_range_reads_are_exact,
            "range returns the exact span",
        ),
        (
            WrongInventoryStorage,
            checks.check_artifact_storage_inventory_is_bounded,
            "continuation starts after",
        ),
        (
            WrongRetirementStorage,
            checks.check_artifact_storage_staging_lifetime,
            "an actual reader keeps cleanup unresolved",
        ),
    ],
)
def test_shared_storage_checks_reject_incorrect_results(
    provider_type: type[LoopBoundStorage], case: StorageCheck, message: str
) -> None:
    async def scenario(root: Path, capture: Capture) -> LoopBoundStorage:
        provider = provider_type(configuration(root))

        try:
            with pytest.raises(pytest.fail.Exception, match=message):
                await case(provider, context_for(capture))
        finally:
            await provider.aclose()

        provider.assert_closed()
        return provider

    with TemporaryDirectory(prefix="kora-storage-negative-") as directory:
        root = Path(directory)
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "storage-negative", utc_now
        ) as capture:
            with asyncio.Runner() as runner:
                provider = runner.run(scenario(root, capture))

    assert not root.exists()
    provider.assert_closed()
    assert_capture_released(capture)


@pytest.mark.parametrize("ending", ["refusal", "cancel", "unexpected"])
def test_shared_storage_check_releases_every_ending(ending: str) -> None:
    async def scenario(root: Path, capture: Capture) -> LoopBoundStorage:
        provider_type = (
            RefusingReaderStorage
            if ending == "refusal"
            else FailingReaderStorage
            if ending == "unexpected"
            else LoopBoundStorage
        )
        provider = provider_type(configuration(root))
        barrier = StorageBarrierActivity("storage.get_range.admitted", on_exit=True)
        context = context_for(capture)

        try:
            if ending == "cancel":
                context = replace(context, activity=barrier)
                task = asyncio.create_task(
                    checks.check_artifact_storage_range_lifetime(provider, context)
                )
                try:
                    await asyncio.wait_for(barrier.entered.wait(), timeout=5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    barrier.released.set()
                    if not task.done():
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
            elif ending == "refusal":
                with pytest.raises(KoraProtocolError, match="storage_unavailable"):
                    await checks.check_artifact_storage_range_lifetime(
                        provider, context
                    )
            else:
                with pytest.raises(ValueError, match="reader handoff failed"):
                    await checks.check_artifact_storage_range_lifetime(
                        provider, context
                    )
        finally:
            await provider.aclose()

        provider.assert_closed()
        return provider

    with TemporaryDirectory(prefix="kora-storage-ending-") as directory:
        root = Path(directory)
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "storage-ending", utc_now
        ) as capture:
            with asyncio.Runner() as runner:
                provider = runner.run(scenario(root, capture))

    assert not root.exists()
    provider.assert_closed()
    assert_capture_released(capture)


@pytest.mark.parametrize("store_name", ["store-a", "store-b"])
def test_shared_storage_failure_and_logging_keep_supplied_markers(
    store_name: str,
) -> None:
    async def scenario(root: Path, captures: list[Capture]) -> LoopBoundStorage:
        configured: JsonObject = {
            "root": str(root / store_name),
            "limits": {"version": 1},
        }
        provider = LoopBoundStorage(
            LocalStorageConfiguration.model_validate(configured)
        )
        markers: list[str] = []

        async def failure(
            selected: ArtifactStorage,
            supplied: JsonObject,
            context: ProtocolContext,
            marker: str,
        ) -> object:
            assert selected is provider
            assert supplied == configured
            markers.append(marker)
            spec = stage_spec(selected.identity(), marker, b"")
            request = operation(
                selected.identity(), "invalid-binding", "begin_stage", spec
            )
            changed = spec.model_copy(update={"content_type": "changed"})
            return await selected.begin_stage(request, changed, context)

        async def configuration_effect(
            selected: ArtifactStorage,
            supplied: JsonObject,
            context: ProtocolContext,
        ) -> None:
            assert selected is provider
            expected = LocalStorageConfiguration.model_validate(supplied)

            await seal_bytes(selected, "configured", b"configured-bytes", context)

            assert provider.control_state().root.path == Path(expected.root).resolve()
            assert (Path(expected.root) / "control.sqlite").is_file()

        inputs = checks.ArtifactStorageConformanceInputs(failure, configuration_effect)
        exercise = partial(inputs.failure, provider, configured)

        try:
            for logging in (False, True):
                with captured(
                    CONFORMANCE_COMPONENT, LoggingSettings(), "storage-marker", utc_now
                ) as capture:
                    captures.append(capture)
                    context = replace(
                        context_for(capture), namespace_id="storage-marker"
                    )
                    if logging:
                        await check_provider_logs_through_its_context(
                            exercise, capture, context, MARKER
                        )
                    else:
                        await inputs.configuration_effect(provider, configured, context)
                        await check_failure_is_a_bounded_protocol_error(
                            exercise, context, MARKER
                        )
                    assert provider.contexts[-1] is context
        finally:
            await provider.aclose()

        assert markers == [MARKER, MARKER]
        provider.assert_closed()
        return provider

    captures: list[Capture] = []
    with TemporaryDirectory(prefix="kora-storage-marker-") as directory:
        root = Path(directory)
        with asyncio.Runner() as runner:
            provider = runner.run(scenario(root, captures))

    assert not root.exists()
    provider.assert_closed()
    for capture in captures:
        assert_capture_released(capture)


@pytest.mark.parametrize(
    ("case", "ending"),
    [
        ("round-trip", "success"),
        ("replay", "success"),
        ("cleanup", "success"),
        ("replay", "wrong-outcome"),
        ("cleanup", "cancel"),
    ],
)
def test_shared_storage_reconstruction_owns_exact_receivers(
    case: str, ending: str
) -> None:
    async def scenario(root: Path, capture: Capture) -> list[LoopBoundStorage]:
        configured = configuration(root)
        receivers: list[LoopBoundStorage] = []
        barrier = StorageBarrierActivity("storage.get.admitted", on_exit=True)
        context = replace(context_for(capture), namespace_id="storage-reconstruction")
        if ending == "cancel":
            context = replace(context, activity=barrier)

        def construct() -> ArtifactStorage:
            if receivers:
                if case == "round-trip":
                    assert not receivers[0].control_state().root.closed
                    assert not receivers[0].worker.done()
                else:
                    receivers[-1].assert_closed()
            provider_type = (
                WrongPromotionStorage
                if receivers and ending == "wrong-outcome"
                else LoopBoundStorage
            )
            receiver = provider_type(configured)
            assert all(receiver is not previous for previous in receivers)
            receivers.append(receiver)
            return receiver

        primary = construct()

        async def exercise() -> None:
            if case == "round-trip":
                await checks.check_artifact_storage_staging_round_trip(
                    primary, context, construct, 1
                )
            elif case == "replay":
                await checks.check_artifact_storage_promotion_replay_preserves_outcome(
                    primary, context, construct
                )
            else:
                await checks.check_artifact_storage_staging_cleanup_is_confined(
                    primary, context, construct
                )

        try:
            if ending == "cancel":
                task = asyncio.create_task(exercise())
                try:
                    await asyncio.wait_for(barrier.entered.wait(), timeout=5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    barrier.released.set()
                    if not task.done():
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
            elif ending == "wrong-outcome":
                with pytest.raises(
                    pytest.fail.Exception,
                    match="a reconstructed receiver preserves the exact saved outcome",
                ):
                    await exercise()
            else:
                await exercise()
        finally:
            for receiver in receivers:
                await receiver.aclose()

        assert len(receivers) == 2
        assert receivers[0].identity() == receivers[1].identity()
        for receiver in receivers:
            assert receiver.contexts
            assert all(received is context for received in receiver.contexts)
            assert receiver.control_state().root.path == Path(configured.root).resolve()
            receiver.assert_closed()
        return receivers

    with TemporaryDirectory(prefix="kora-storage-reconstruction-") as directory:
        root = Path(directory)
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "storage-reconstruction", utc_now
        ) as capture:
            with asyncio.Runner() as runner:
                receivers = runner.run(scenario(root, capture))

    assert not root.exists()
    for receiver in receivers:
        receiver.assert_closed()
    assert_capture_released(capture)
