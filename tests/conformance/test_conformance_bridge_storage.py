import asyncio
import json
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest

from kora.conformance import artifact_storage as checks
from kora.conformance.artifact_storage import (
    check_artifact_storage_promotion_replay_preserves_outcome as check_replay,
)
from kora.conformance.bridge import (
    ConformanceBridgeError,
    ConformanceSelection,
    ConstructorArguments,
    PreparedConformance,
    PreparedInvocation,
)
from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.conformance.inputs import ConformanceInputError
from kora.conformance.storage_support import operation, seal_bytes, stage_spec
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorage
from tests.conformance.lifetime_support import owned_lifetime
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.conformance.test_shared_storage_checks import LoopBoundStorage
from tests.plugins.storage.activity import StorageBarrierActivity


@pytest.fixture
def storage_root() -> Generator[Path]:
    with TemporaryDirectory(prefix="kora-bridge-storage-") as directory:
        root = Path(directory)
        yield root
    assert not root.exists()


async def failure(
    provider: ArtifactStorage, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    assert config["root"]
    spec = stage_spec(provider.identity(), marker, b"")
    request = operation(provider.identity(), "invalid-binding", "begin_stage", spec)
    return await provider.begin_stage(
        request, spec.model_copy(update={"content_type": "changed"}), context
    )


async def effect(
    provider: ArtifactStorage, config: JsonObject, context: ProtocolContext
) -> None:
    assert config["root"]
    await seal_bytes(provider, "configured", b"configured-bytes", context)


class ObservedStorage(LoopBoundStorage):
    def __init__(
        self,
        configuration: LocalStorageConfiguration,
        phase: str,
        index: int,
        closed: list[int],
    ) -> None:
        super().__init__(configuration)
        self.phase = phase
        self.index = index
        self.closed = closed
        if index and phase == "schema":
            self.config_schema = {**self.config_schema, "title": "different"}

    async def aclose(self) -> None:
        await super().aclose()
        self.closed.append(self.index)
        if self.index and self.phase == "close":
            raise RuntimeError("auxiliary close failed")


class StorageAdapter:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.bindings: list[JsonObject] = []
        self.receivers: list[ObservedStorage] = []
        self.cleaned: list[object | None] = []
        self.closed: list[int] = []
        self.exercises = checks.ArtifactStorageConformanceInputs(failure, effect)

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        self.bindings.append(dict(config))
        configuration = LocalStorageConfiguration.model_validate(config)
        config.clear()
        return ConstructorArguments((configuration,))

    def construct(self, configuration: LocalStorageConfiguration) -> object:
        if self.receivers and self.phase == "alias-primary":
            return self.receivers[0]
        if len(self.receivers) == 2 and self.phase == "alias-auxiliary":
            return self.receivers[1]
        if self.receivers and self.phase in {"replay", "cleanup", "cancel"}:
            self.receivers[-1].assert_closed()
        receiver = ObservedStorage(
            configuration, self.phase, len(self.receivers), self.closed
        )
        self.receivers.append(receiver)
        return receiver

    async def cleanup(self, instance: object | None) -> None:
        self.cleaned.append(instance)
        if instance is None:
            assert not self.receivers
            return
        for receiver in self.receivers[1:]:
            receiver.assert_closed()
        assert instance is self.receivers[0]
        await self.receivers[0].aclose()


def selected(adapter: StorageAdapter, root: Path) -> ConformanceSelection:
    metadata = ProviderMetadata(
        ProtocolName.ARTIFACT_STORAGE,
        "local",
        adapter.construct,
        LocalArtifactStorage.config_schema,
        "1",
    )
    return ConformanceSelection(
        metadata, json.dumps({"root": str(root), "limits": {"version": 1}}).encode()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["round-trip", "replay", "cleanup", "cancel"])
async def test_bridge_storage_reconstruction_closes_actual_receivers(
    tmp_path: Path, storage_root: Path, phase: str
) -> None:
    adapter = StorageAdapter(phase)
    root = storage_root / "store"
    selection = selected(adapter, root)
    barrier = StorageBarrierActivity("storage.get.admitted", on_exit=True)
    retained: list[PreparedInvocation] = []

    async with owned_lifetime(tmp_path) as lifetime:
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "bridge-storage", utc_now
        ) as capture:
            context = context_for(capture)
            if phase == "cancel":
                context = replace(context, activity=barrier)

            async def exercise() -> None:
                async with selection.prepare(adapter, lifetime) as invocation:
                    retained.append(invocation)
                    provider = cast("ArtifactStorage", invocation.instance)
                    if phase == "round-trip":
                        await checks.check_artifact_storage_staging_round_trip(
                            provider, context, invocation.reconstruct, 1
                        )
                    elif phase == "replay":
                        await check_replay(provider, context, invocation.reconstruct)
                    else:
                        await checks.check_artifact_storage_staging_cleanup_is_confined(
                            provider, context, invocation.reconstruct
                        )

            if phase == "cancel":
                task = asyncio.create_task(exercise())
                boundary = asyncio.create_task(barrier.entered.wait())
                try:
                    finished, _ = await asyncio.wait(
                        (task, boundary), timeout=5, return_when=asyncio.FIRST_COMPLETED
                    )
                    if task in finished:
                        task.result()
                    assert boundary in finished
                    assert adapter.receivers[1].resource_counts()["readers"] > 0
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                finally:
                    barrier.released.set()
                    boundary.cancel()
                    await asyncio.gather(boundary, return_exceptions=True)
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            else:
                await exercise()

    assert len(adapter.receivers) == len(adapter.bindings) == 2
    assert adapter.bindings[0] == adapter.bindings[1]
    assert adapter.cleaned == [adapter.receivers[0]]
    assert adapter.closed[-1] == 0
    for receiver in adapter.receivers:
        receiver.assert_closed()
        assert receiver.control_state().root.path == root.resolve()
    with pytest.raises(ConformanceBridgeError, match="invocation_closed"):
        retained[0].reconstruct()
    assert_capture_released(capture)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["alias-primary", "alias-auxiliary", "schema", "close"]
)
async def test_bridge_storage_refusals_keep_existing_cleanup_owners(
    tmp_path: Path, storage_root: Path, phase: str
) -> None:
    adapter = StorageAdapter(phase)
    selection = selected(adapter, storage_root / "store")
    expected = ValueError

    async with owned_lifetime(tmp_path) as lifetime:

        async def exercise() -> None:
            async with selection.prepare(adapter, lifetime) as invocation:
                if phase == "alias-auxiliary":
                    invocation.reconstruct()
                invocation.reconstruct()
                if phase == "close":
                    raise ValueError("exercise failed")

        with pytest.raises(expected) as raised:
            await exercise()

    assert adapter.cleaned == [adapter.receivers[0]]
    assert adapter.closed[-1] == 0
    for receiver in adapter.receivers:
        receiver.assert_closed()
    if phase.startswith("alias"):
        assert isinstance(raised.value, ConformanceBridgeError)
        assert str(raised.value) == "receiver_repeated"
        assert adapter.closed.count(1) == (phase == "alias-auxiliary")
    elif phase == "schema":
        assert isinstance(raised.value, ConformanceInputError)
        assert len(adapter.receivers) == 2
    else:
        assert str(raised.value) == "exercise failed"
        assert isinstance(raised.value.__cause__, RuntimeError)
