from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import (
    MutationReceipt,
    StageReleaseRequest,
    StorageLimits,
    StorageMutationSet,
)
from tests.plugins.storage.support import operation, stage_spec


def test_local_storage_stage_receipt_survives_restart_and_retires(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    async def scenario() -> None:
        configuration = LocalStorageConfiguration(
            root=str(tmp_path / "store"), limits=StorageLimits(version=1, stage_slots=1)
        )
        context = context_for(kora_capture)
        provider = LocalArtifactStorage(configuration)
        spec = stage_spec(provider.identity(), "stage-1", b"data")
        begin = operation(provider.identity(), "begin-1", "begin_stage", spec)

        stage = await provider.begin_stage(begin, spec, context)
        await provider.aclose()
        restarted = LocalArtifactStorage(configuration)
        replayed = await restarted.begin_stage(begin, spec, context)
        retire = operation(restarted.identity(), "retire-1", "retire_stage", stage)
        retired = await restarted.retire_stage(retire, stage, context)
        observed = await restarted.observe(begin, context)
        receipts = tuple(
            sorted(
                (
                    MutationReceipt(
                        version=1,
                        operation_id=op.operation_id,
                        kind=op.kind,
                        request_digest=op.request_digest,
                        outcome_digest=result.metadata_digest(),
                    )
                    for op, result in ((begin, stage), (retire, retired))
                ),
                key=lambda item: item.operation_id,
            )
        )
        acknowledged = StorageMutationSet(version=1, stage=stage, receipts=receipts)
        release = StageReleaseRequest(
            version=1,
            stage=stage,
            persisted_outcome_revision="outcome-1",
            mutation_set_digest=acknowledged.metadata_digest(),
            issuance_closed="closed-issuers-1",
        )
        released = await restarted.release_stage(release, context)
        unavailable = await restarted.observe(begin, context)
        late = stage_spec(provider.identity(), "late", b"x")
        try:
            with pytest.raises(KoraProtocolError, match="storage_issuer_closed"):
                await restarted.begin_stage(
                    operation(restarted.identity(), "late-begin", "begin_stage", late),
                    late,
                    context,
                )
        finally:
            await restarted.aclose()

        assert replayed == stage
        assert retired.completed
        assert observed.state == "settled"
        assert observed.result == stage
        assert released.state == "released"
        assert unavailable.state == "evidence_unavailable"

    asyncio.run(scenario())


@pytest.mark.parametrize("cancellations", [1, 2])
def test_cancelled_native_result_closes_transferred_lease(
    tmp_path: Path, cancellations: int
) -> None:
    import asyncio
    import threading

    from kora.plugins.storage.files import FileLease, RootAnchor

    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        anchor = RootAnchor.open(tmp_path / "store", StorageLimits(version=1))
        started = threading.Event()
        finish = threading.Event()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def acquire() -> FileLease:
            lease = anchor.try_lock("data-0")
            assert lease is not None
            started.set()
            loop.call_soon_threadsafe(entered.set)
            assert finish.wait(timeout=5)
            return lease

        job = asyncio.create_task(provider.io(acquire, on_cancel=FileLease.close))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            for _ in range(cancellations):
                job.cancel()
                cancellation_delivered = loop.create_future()
                loop.call_soon(cancellation_delivered.set_result, None)
                await cancellation_delivered
            assert not job.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await job
            available = anchor.try_lock("data-0")
            assert available is not None
            available.close()
        finally:
            finish.set()
            await provider.aclose()

        anchor.close()
        assert started.is_set()

    asyncio.run(scenario())


def test_local_storage_immutable_part_seal_and_prefix(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    from kora.protocols.storage_models import (
        ManifestPrefix,
        PrefixCapture,
        StageManifest,
        StageWrite,
    )
    from tests.plugins.storage.support import BytesSource, single_manifest, single_part

    async def scenario() -> None:
        configuration = LocalStorageConfiguration(
            root=str(tmp_path / "store"), limits=StorageLimits(version=1)
        )
        provider = LocalArtifactStorage(configuration)
        context = context_for(kora_capture)
        payload = b"immutable-data" * 8192
        spec = stage_spec(provider.identity(), "complete", payload)
        stage = await provider.begin_stage(
            operation(provider.identity(), "begin", "begin_stage", spec), spec, context
        )
        part_spec = single_part(payload)
        write = operation(
            provider.identity(),
            "part",
            "write_part",
            StageWrite(version=1, stage=stage, spec=part_spec),
        )
        source = BytesSource(payload)
        try:
            part = await provider.write_part(write, stage, part_spec, source, context)
            manifest = single_manifest(stage, part_spec, part.part_identity)
            seal_operation = operation(
                provider.identity(), "seal", "seal_stage", manifest
            )
            sealed = await provider.seal_stage(seal_operation, stage, manifest, context)
            other_stage = stage.model_copy(update={"stage_id": "different-stage"})
            with pytest.raises(KoraProtocolError, match="storage_conflict"):
                await provider.seal_stage(
                    seal_operation, other_stage, manifest, context
                )
            reread = await provider.read_stage(sealed, context)
            chunks = [chunk async for chunk in reread]
            await reread.aclose()
            replay = await provider.write_part(
                write, stage, part_spec, BytesSource(payload), context
            )
            with pytest.raises(KoraProtocolError, match="storage_conflict"):
                await provider.write_part(
                    write, stage, part_spec, BytesSource(b"changed"), context
                )
            prefix_spec = stage_spec(provider.identity(), "prefix", payload)
            prefix_stage = await provider.begin_stage(
                operation(
                    provider.identity(), "prefix-begin", "begin_stage", prefix_spec
                ),
                prefix_spec,
                context,
            )
            prefix_source = BytesSource(payload)
            capture = PrefixCapture(
                version=1, stage=prefix_stage, source_spec=part_spec, count=3
            )
            prefix = await provider.capture_prefix(
                operation(
                    provider.identity(), "prefix-write", "capture_prefix", capture
                ),
                prefix_stage,
                part_spec,
                3,
                prefix_source,
                context,
            )
            incomplete_manifest = StageManifest(
                version=1,
                stage=prefix_stage,
                parts=(),
                prefix=ManifestPrefix(
                    version=1,
                    source_spec=part_spec,
                    count=prefix.count,
                    digest=prefix.digest,
                    private_identity=prefix.private_identity,
                ),
            )
            incomplete = await provider.seal_stage(
                operation(
                    provider.identity(),
                    "prefix-seal",
                    "seal_stage",
                    incomplete_manifest,
                ),
                prefix_stage,
                incomplete_manifest,
                context,
            )
        finally:
            await provider.aclose()

        assert b"".join(chunks) == payload
        assert max(map(len, chunks)) <= 65536
        assert replay == part
        assert source.closed
        assert prefix_source.closed
        assert prefix_source.offset == 3
        assert incomplete.total_size == 3
        assert not incomplete.source_complete

    asyncio.run(scenario())


def test_local_storage_private_cleanup_closes_readers_and_failed_parts(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    from kora.protocols.storage_models import NoEffectFailure, StageWrite
    from tests.plugins.storage.support import BytesSource, single_manifest, single_part

    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        payload = b"retained reader"
        spec = stage_spec(provider.identity(), "readers", payload)
        stage = await provider.begin_stage(
            operation(provider.identity(), "begin", "begin_stage", spec), spec, context
        )
        part_spec = single_part(payload)
        write = operation(
            provider.identity(),
            "write",
            "write_part",
            StageWrite(version=1, stage=stage, spec=part_spec),
        )
        part = await provider.write_part(
            write, stage, part_spec, BytesSource(payload), context
        )
        manifest = single_manifest(stage, part_spec, part.part_identity)
        sealed = await provider.seal_stage(
            operation(provider.identity(), "seal", "seal_stage", manifest),
            stage,
            manifest,
            context,
        )
        reader = await provider.read_stage(sealed, context)
        retirement = operation(provider.identity(), "retire", "retire_stage", stage)
        try:
            blocked = await provider.retire_stage(retirement, stage, context)
            with pytest.raises(KoraProtocolError, match="storage_unavailable"):
                await provider.read_stage(sealed, context)
            await reader.aclose()
            completed = await provider.retire_stage(retirement, stage, context)
            failed_spec = stage_spec(provider.identity(), "failed", payload)
            failed_stage = await provider.begin_stage(
                operation(
                    provider.identity(), "failed-begin", "begin_stage", failed_spec
                ),
                failed_spec,
                context,
            )
            failed_write = operation(
                provider.identity(),
                "failed-write",
                "write_part",
                StageWrite(version=1, stage=failed_stage, spec=part_spec),
            )
            with pytest.raises(KoraProtocolError, match="storage_conflict"):
                await provider.write_part(
                    failed_write, failed_stage, part_spec, BytesSource(b"bad"), context
                )
            failed_retirement = operation(
                provider.identity(), "failed-retire", "retire_stage", failed_stage
            )
            cleaned = await provider.retire_stage(
                failed_retirement, failed_stage, context
            )
            failure = await provider.observe(failed_write, context)
        finally:
            await reader.aclose()
            await provider.aclose()

        assert not blocked.completed
        assert completed.completed
        assert cleaned.completed
        assert failure.state == "settled"
        assert isinstance(failure.result, NoEffectFailure)
        assert list((tmp_path / "store" / "stages").iterdir()) == []

    asyncio.run(scenario())


def test_local_storage_refuses_replaced_control_inode(tmp_path: Path) -> None:
    import asyncio
    import shutil

    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )
    provider = LocalArtifactStorage(configuration)
    asyncio.run(provider.aclose())
    database = tmp_path / "store" / "control.sqlite"
    original = tmp_path / "original.sqlite"
    database.rename(original)
    shutil.copyfile(original, database)
    database.chmod(0o600)

    with pytest.raises(KoraProtocolError, match="storage_identity"):
        LocalArtifactStorage(configuration)


def test_local_storage_cancelled_reader_handoff_closes_undelivered_stream(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio
    from dataclasses import replace

    from kora.protocols.storage_models import StageWrite
    from tests.plugins.storage.activity import ReaderHandoffActivity
    from tests.plugins.storage.support import BytesSource, single_manifest, single_part

    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        activity = ReaderHandoffActivity()
        context = replace(context_for(kora_capture), activity=activity)
        payload = b"handoff"
        spec = stage_spec(provider.identity(), "stage", payload)
        stage = await provider.begin_stage(
            operation(provider.identity(), "begin", "begin_stage", spec), spec, context
        )
        part_spec = single_part(payload)
        write = operation(
            provider.identity(),
            "part",
            "write_part",
            StageWrite(version=1, stage=stage, spec=part_spec),
        )
        part = await provider.write_part(
            write, stage, part_spec, BytesSource(payload), context
        )
        manifest = single_manifest(stage, part_spec, part.part_identity)
        sealed = await provider.seal_stage(
            operation(provider.identity(), "seal", "seal_stage", manifest),
            stage,
            manifest,
            context,
        )

        read = asyncio.create_task(provider.read_stage(sealed, context))
        try:
            await asyncio.wait_for(activity.undelivered.wait(), timeout=5)
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
            await asyncio.wait_for(activity.reader_closed.wait(), timeout=5)
            retirement = await provider.retire_stage(
                operation(provider.identity(), "retire", "retire_stage", stage),
                stage,
                context,
            )
        finally:
            activity.release_handoff.set()
            await provider.aclose()

        assert retirement.completed

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [-1, 3, True])
def test_local_storage_invalid_prefix_is_typed_and_closes_source(
    tmp_path: Path, kora_capture: Capture, count: int
) -> None:
    import asyncio

    from kora.protocols.storage_models import PrefixCapture
    from tests.plugins.storage.support import BytesSource, single_part

    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        spec = stage_spec(provider.identity(), "prefix", b"abc")
        part_spec = single_part(b"abc")
        arguments = PrefixCapture(
            version=1, stage=spec.stage, source_spec=part_spec, count=1
        )
        request = operation(provider.identity(), "prefix", "capture_prefix", arguments)
        source = BytesSource(b"abc")

        try:
            with pytest.raises(KoraProtocolError, match="storage_conflict"):
                await provider.capture_prefix(
                    request, spec.stage, part_spec, count, source, context
                )
        finally:
            await provider.aclose()

        assert source.closed
        assert source.calls == 0

    asyncio.run(scenario())


def test_closed_receiver_closes_an_unconsumed_input(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    from kora.protocols.storage_models import StageWrite
    from tests.plugins.storage.support import BytesSource, single_part

    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        spec = stage_spec(provider.identity(), "stage", b"bytes")
        part_spec = single_part(b"bytes")
        write = operation(
            provider.identity(),
            "write",
            "write_part",
            StageWrite(version=1, stage=spec.stage, spec=part_spec),
        )
        source = BytesSource(b"bytes")
        await provider.aclose()

        with pytest.raises(KoraProtocolError, match="storage_closed"):
            await provider.write_part(write, spec.stage, part_spec, source, context)

        assert source.closed
        assert source.calls == 0

    asyncio.run(scenario())
