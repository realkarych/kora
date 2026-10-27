import asyncio
import os
import stat
import tempfile
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import BytesSource, digest, operation, seal_bytes
from kora.plugins.storage.files import RootAnchor
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import (
    ContentPreparation,
    PreparationArguments,
    StorageLimits,
)

UNPRIVILEGED_UID: Final[int] = 65534


def unprivileged_owner() -> int:
    effective = os.geteuid()
    return UNPRIVILEGED_UID if effective == 0 else effective


def test_private_cleanup_failure_retains_intent_until_directory_closes(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = tmp_path / "store"
    context = context_for(kora_capture)

    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(root=str(root), limits=StorageLimits(version=1))
        )
        try:
            sealed, _, _ = await seal_bytes(provider, "cleanup", b"raw", context)
            arguments = PreparationArguments(
                version=1,
                source=sealed,
                maximum_output_size=16,
                expected_output_digest=digest(b"prepared"),
            )
            request = ContentPreparation(
                version=1,
                operation=operation(
                    provider.identity(), "prepare", "prepare_content", arguments
                ),
                arguments=arguments,
            )
            await provider.prepare_content(request, BytesSource(b"prepared"), context)
            private = next((root / "stages").glob("*/part-0")).parent
            blocker = private / "test-owned-unregistered"
            blocker.write_bytes(b"held")
            retirement = operation(
                provider.identity(), "retire", "retire_stage", sealed.stage
            )

            try:
                with pytest.raises(KoraProtocolError) as retained:
                    await provider.retire_stage(retirement, sealed.stage, context)
                observed = await provider.observe(retirement, context)
                inventory = await provider.scan("stages", None, 128, context)

                assert retained.value.code == "storage_io"
                assert observed.state == "pending"
                assert inventory.entries[0].size > 0
                assert tuple(private.iterdir()) == (blocker,)
            finally:
                blocker.unlink()
            completed = await provider.retire_stage(retirement, sealed.stage, context)

            assert completed.completed
            assert not private.exists()
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("target", "code"),
    [("directory", "storage_io"), ("leaf", "storage_path")],
    ids=["directory", "leaf"],
)
def test_retirement_refuses_payload_symlink_substitution(
    tmp_path: Path, kora_capture: Capture, target: str, code: str
) -> None:
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    foreign = outside / "part-0"
    foreign.write_bytes(b"foreign content")
    context = context_for(kora_capture)

    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(root=str(root), limits=StorageLimits(version=1))
        )
        try:
            sealed, _, _ = await seal_bytes(provider, "confined", b"owned", context)
            private = next((root / "stages").glob("*/part-0"))
            selected = private.parent if target == "directory" else private
            saved = tmp_path / "saved-private"
            selected.rename(saved)
            selected.symlink_to(outside if target == "directory" else foreign)
            retirement = operation(
                provider.identity(), "retire", "retire_stage", sealed.stage
            )

            try:
                with pytest.raises(KoraProtocolError) as refused:
                    await provider.retire_stage(retirement, sealed.stage, context)

                assert refused.value.code == code
                assert selected.is_symlink()
                assert foreign.read_bytes() == b"foreign content"
                assert tuple(outside.iterdir()) == (foreign,)
            finally:
                selected.unlink()
                saved.rename(selected)
            completed = await provider.retire_stage(retirement, sealed.stage, context)

            assert completed.completed
            assert foreign.read_bytes() == b"foreign content"
        finally:
            await provider.aclose()

    asyncio.run(scenario())


def test_root_owned_sticky_ancestor_protects_owned_entry() -> None:
    with tempfile.TemporaryDirectory(prefix="kora-sticky-", dir="/tmp") as directory:
        path = Path(directory) / "store"
        shared = Path(directory).resolve().parent.stat()
        assert shared.st_uid == 0, "setup requires /tmp to be owned by root"
        assert stat.S_IMODE(shared.st_mode) == 0o1777, (
            "setup requires /tmp to be a sticky world-writable directory"
        )

        with RootAnchor.open(path, StorageLimits(version=1)) as root:
            observed = root.path

        assert observed == path.resolve()


def test_user_owned_sticky_ancestor_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    os.chown(shared, unprivileged_owner(), -1)
    shared.chmod(0o1777)
    owner = shared.stat()
    assert owner.st_uid != 0
    assert stat.S_IMODE(owner.st_mode) == 0o1777

    with pytest.raises(KoraProtocolError) as refused:
        RootAnchor.open(shared / "store", StorageLimits(version=1))

    assert refused.value.code == "storage_path"
