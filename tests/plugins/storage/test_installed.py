import asyncio
from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import operation, stage_spec
from kora.plugins.storage.local import LocalArtifactStorage
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from kora.protocols.storage_models import StorageLimits


def test_installed_local_storage_loads_and_uses_validated_configuration(
    tmp_path: Path, kora_capture: Capture
) -> None:
    metadata = load_provider(
        ProtocolName.ARTIFACT_STORAGE, "local", kora_capture.diagnostics
    )

    created = metadata.implementation(
        {
            "root": str(tmp_path / "store"),
            "limits": StorageLimits(version=1).model_dump(mode="json"),
        }
    )
    assert isinstance(created, LocalArtifactStorage)
    context = context_for(kora_capture)
    spec = stage_spec(created.identity(), "installed", b"")

    async def scenario() -> None:
        try:
            stage = await created.begin_stage(
                operation(created.identity(), "installed-begin", "begin_stage", spec),
                spec,
                context,
            )
        finally:
            await created.aclose()

        assert stage == spec.stage

    asyncio.run(scenario())
