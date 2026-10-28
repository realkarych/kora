import asyncio
import fcntl
import os
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import seal_bytes
from kora.plugins.storage.files import RootAnchor
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.plugins.storage.readers import ReaderManager
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageLimits
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.configuration import local_configuration


def test_reader_ticket_closes_when_owner_lock_refuses(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configured = tmp_path / "store"
        limits = StorageLimits(version=1)
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(root=str(configured), limits=limits)
        )
        with RootAnchor.open(configured, limits) as anchor:
            readers = ReaderManager(anchor, provider.io, provider.start_read)
            (configured / "locks" / "stage-0").unlink()

            try:
                with pytest.raises(KoraProtocolError) as refused:
                    await readers.open(
                        (), "stage-0", lambda: None, 0, 0, context_for(kora_capture)
                    )
                available = anchor.try_lock("reader-0")
            finally:
                await provider.aclose()

            assert refused.value.code == "storage_path"
            assert available is not None
            available.close()
            assert readers.handles == 0
            assert not readers.streams

    asyncio.run(scenario())


def test_stream_reads_obey_root_data_work_admission(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configured = tmp_path / "store"
        limits = StorageLimits(version=1)
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(root=str(configured), limits=limits)
        )
        context = context_for(kora_capture)
        sealed, _, _ = await seal_bytes(provider, "read-admission", b"bytes", context)
        reader = await provider.read_stage(sealed, context)
        with RootAnchor.open(configured, limits) as anchor:
            held = tuple(
                anchor.try_lock(f"data-{index}")
                for index in range(limits.data_work_slots)
            )

            try:
                assert all(lease is not None for lease in held)
                with pytest.raises(KoraProtocolError) as refused:
                    await reader.read(1)
                assert refused.value.code == "storage_busy"
            finally:
                for lease in held:
                    if lease is not None:
                        lease.close()
                await reader.aclose()
                await provider.aclose()

    asyncio.run(scenario())


def test_reader_validation_refusal_closes_both_acquired_leases(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configured = tmp_path / "store"
        limits = StorageLimits(version=1)
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(root=str(configured), limits=limits)
        )
        with RootAnchor.open(configured, limits) as anchor:
            readers = ReaderManager(anchor, provider.io, provider.start_read)
            (configured / "manifest.json").write_bytes(b'{"version":1}')

            try:
                with pytest.raises(KoraProtocolError) as refused:
                    await readers.open(
                        (),
                        "stage-0",
                        anchor.refresh_manifest,
                        0,
                        0,
                        context_for(kora_capture),
                    )
                reader = anchor.try_lock("reader-0")
                owner = anchor.try_lock("stage-0")
                try:
                    assert refused.value.code == "storage_identity"
                    assert reader is not None
                    assert owner is not None
                    assert readers.handles == 0
                    assert not readers.streams
                finally:
                    if owner is not None:
                        owner.close()
                    if reader is not None:
                        reader.close()
            finally:
                await provider.aclose()

    asyncio.run(scenario())


def test_provider_shutdown_closes_all_readers_after_read_failure(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configured = tmp_path / "store"
        provider = LocalArtifactStorage(local_configuration(configured))
        context = context_for(kora_capture)
        sealed, _, _ = await seal_bytes(provider, "failed", b"readable", context)
        other, _, _ = await seal_bytes(provider, "other", b"second", context)
        barrier = StorageBarrierActivity("storage.reader.read")
        broken = await provider.read_stage(sealed, replace(context, activity=barrier))
        retained = await provider.read_stage(other, context)
        reading = asyncio.create_task(broken.read(1))
        await asyncio.wait_for(barrier.entered.wait(), timeout=5)
        (configured / "stages" / "0" / "part-0").write_bytes(b"")

        closing = asyncio.create_task(provider.aclose())
        await asyncio.wait_for(barrier.close_started.wait(), timeout=5)
        barrier.released.set()
        with pytest.raises(KoraProtocolError) as failed_read:
            await reading
        with pytest.raises(BaseExceptionGroup) as closure:
            await asyncio.wait_for(closing, timeout=5)
        lifetime = os.open(configured / "locks" / "lifetime", os.O_RDWR)
        try:
            fcntl.flock(lifetime, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lifetime)

        assert failed_read.value.code == "storage_integrity"
        assert [type(failure) for failure in closure.value.exceptions] == [
            KoraProtocolError
        ]
        assert [
            failure.code
            for failure in closure.value.exceptions
            if isinstance(failure, KoraProtocolError)
        ] == ["storage_integrity"]
        assert await retained.read(1) == b""

    asyncio.run(scenario())
