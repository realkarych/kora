import asyncio
import sqlite3
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import operation, stage_spec
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageLimits


def test_provider_private_control_refuses_existing_metadata_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "metadata"
    root.mkdir(mode=0o700)
    database = root / "metadata.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE run (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO run(id) VALUES ('retained')")
        connection.commit()
    finally:
        connection.close()
    before = database.read_bytes()
    configuration = LocalStorageConfiguration(
        root=str(root), limits=StorageLimits(version=1)
    )

    with pytest.raises(KoraProtocolError) as refused:
        LocalArtifactStorage(configuration)

    assert refused.value.code == "storage_identity"
    assert database.read_bytes() == before
    assert not (root / "control.sqlite").exists()


def test_storage_identity_source_is_injected_and_not_reissued_on_restart(
    tmp_path: Path,
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )
    issued: list[str] = []

    def new_identity() -> str:
        identity = f"{len(issued) + 1:032x}"
        issued.append(identity)
        return identity

    async def scenario() -> None:
        provider = LocalArtifactStorage(configuration, new_identity=new_identity)
        identity = provider.identity()
        await provider.aclose()

        restarted = LocalArtifactStorage(configuration, new_identity=new_identity)
        restored = restarted.identity()
        await restarted.aclose()

        assert identity == restored
        assert identity.store_id == "00000000000000000000000000000001"
        assert issued == [identity.store_id]

    asyncio.run(scenario())


@pytest.mark.parametrize("encoded", ['{"private":"corrupt-saved-content"}', "not JSON"])
def test_corrupt_saved_outcome_is_a_content_free_integrity_error(
    tmp_path: Path, kora_capture: Capture, encoded: str
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )
    context = context_for(kora_capture)

    async def scenario() -> None:
        provider = LocalArtifactStorage(configuration)
        spec = stage_spec(provider.identity(), "corrupt", b"")
        begin = operation(provider.identity(), "begin", "begin_stage", spec)
        await provider.begin_stage(begin, spec, context)
        await provider.aclose()
        connection = sqlite3.connect(tmp_path / "store" / "control.sqlite")
        try:
            connection.execute(
                "UPDATE receipts SET result=? WHERE operation_id=?",
                (encoded, begin.operation_id),
            )
            connection.commit()
        finally:
            connection.close()
        restarted = LocalArtifactStorage(configuration)

        try:
            with pytest.raises(KoraProtocolError) as caught:
                await restarted.observe(begin, context)
        finally:
            await restarted.aclose()

        assert caught.value.code == "storage_integrity"
        assert "corrupt-saved-content" not in str(caught.value)
        assert not caught.value.retryable

    asyncio.run(scenario())
