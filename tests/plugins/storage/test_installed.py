import asyncio
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import operation, stage_spec
from kora.plugins.storage.local import LocalArtifactStorage
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from kora.protocols.storage_models import StorageLimits
from tests.plugins.storage.configuration import local_configuration
from tests.support.databases import marker_only_sqlite


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


@pytest.mark.parametrize("marker", [b"KMT1", b"KJT1"])
def test_local_storage_refuses_a_copied_marker_before_its_sidecar_check(
    tmp_path: Path, marker: bytes
) -> None:
    configuration = local_configuration(tmp_path / "store")
    initial = LocalArtifactStorage(configuration)
    asyncio.run(initial.aclose())
    path = Path(configuration.root) / "control.sqlite"
    image = marker_only_sqlite(tmp_path / "marker.sqlite", marker)
    path.write_bytes(image)
    sidecar = path.with_suffix(".sqlite-wal")
    sidecar.write_bytes(b"owned sidecar witness")

    with pytest.raises(KoraProtocolError) as refusal:
        LocalArtifactStorage(configuration)

    assert refusal.value.code == "storage_io"
    assert path.read_bytes() == image
    assert sidecar.read_bytes() == b"owned sidecar witness"
    assert not path.with_suffix(".sqlite-shm").exists()
    assert not path.with_suffix(".sqlite-journal").exists()
