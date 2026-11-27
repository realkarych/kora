import asyncio
import sqlite3
from contextlib import closing
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
from kora.sqlite.driver import Connection
from tests.api.guarded_sqlite_prebind_child import (
    held_main_descriptors,
    native_identity,
)
from tests.plugins.storage.configuration import local_configuration
from tests.support.databases import copy_sqlite_template, marker_only_sqlite


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


def test_local_storage_settles_a_refused_journal_on_the_original_writer(
    tmp_path: Path, sqlite_template: Path
) -> None:
    configuration = local_configuration(tmp_path / "store")
    initial = LocalArtifactStorage(configuration)
    asyncio.run(initial.aclose())
    metadata = tmp_path / "metadata.sqlite"
    copy_sqlite_template(sqlite_template, metadata)
    metadata.chmod(0o600)
    identity = native_identity(metadata)
    root = Path(configuration.root)
    root_gate = native_identity(root / "locks" / "root")
    journal = root / "control.sqlite-journal"
    journal.hardlink_to(metadata)

    with closing(Connection()) as original:
        original.open(metadata, False, 0, lambda descriptor, empty: not empty)
        original.execute("PRAGMA journal_mode=DELETE").fetchone()
        original.execute("CREATE TABLE original_writer (value INTEGER)").close()
        original.execute("BEGIN IMMEDIATE").close()
        original.execute("INSERT INTO original_writer VALUES (52)").close()
        before = held_main_descriptors(identity)

        with pytest.raises(KoraProtocolError) as refusal:
            LocalArtifactStorage(configuration)
        retained = held_main_descriptors(identity)
        retained_gate = held_main_descriptors(root_gate)
        original.commit()
        saved = original.execute("SELECT value FROM original_writer").fetchone()
        with closing(Connection()) as competing:
            with pytest.raises(sqlite3.OperationalError, match="unable to open"):
                competing.open(metadata, False, 0, lambda descriptor, empty: not empty)
        original.close()
        descriptors_after_close = held_main_descriptors(identity)
        gate_after_close = held_main_descriptors(root_gate)
        journal.unlink()
        retry = LocalArtifactStorage(configuration)
        asyncio.run(retry.aclose())

    assert refusal.value.code == "storage_io"
    assert len(before) == 1
    assert len(retained) == 2
    assert set(before) < set(retained)
    assert len(retained_gate) == 1
    assert saved == (52,)
    assert not descriptors_after_close
    assert not gate_after_close
    assert not held_main_descriptors(identity)


def test_local_storage_checks_an_empty_journal_before_sqlite_can_skip_it(
    tmp_path: Path,
) -> None:
    configuration = local_configuration(tmp_path / "store")
    initial = LocalArtifactStorage(configuration)
    asyncio.run(initial.aclose())
    empty = tmp_path / "empty"
    empty.touch(mode=0o600)
    journal = Path(configuration.root) / "control.sqlite-journal"
    journal.hardlink_to(empty)
    identity = native_identity(empty)

    with pytest.raises(KoraProtocolError) as refusal:
        LocalArtifactStorage(configuration)

    assert refusal.value.code == "storage_path"
    assert empty.stat().st_size == 0
    assert journal.stat().st_nlink == 2
    assert not held_main_descriptors(identity)
