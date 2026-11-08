from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import (
    BytesSource,
    acknowledge_stage,
    operation,
    seal_bytes,
    seal_repeated,
    single_part,
    stage_release,
    stage_spec,
    write_single_part,
)
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StageRef, StorageLimits, StorageOperationRef
from tests.measurements.storage_lifetime_resources import close_provider
from tests.plugins.storage.configuration import local_configuration
from tests.plugins.storage.inspection import InspectedStorage


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
        release = stage_release(
            stage,
            ((begin, stage), (retire, retired)),
            persisted_outcome_revision="outcome-1",
            issuance_closed="closed-issuers-1",
        )
        released = await restarted.release_stage(release, context)
        unavailable = await restarted.observe(begin, context)
        late = stage_spec(provider.identity(), "late", b"x")
        try:
            with pytest.raises(KoraProtocolError) as late_begin:
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
        assert late_begin.value.code == "storage_issuer_closed"

    asyncio.run(scenario())


def test_local_storage_64_stage_cohort_retires_at_saturation(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    root = tmp_path / "store"
    configuration = LocalStorageConfiguration(
        root=str(root), limits=StorageLimits(version=1, stage_slots=64)
    )
    context = context_for(kora_capture)
    absent_stage_id = "private-absent-cohort-stage"

    async def scenario() -> None:
        provider = InspectedStorage(configuration)
        try:
            identity = provider.identity()
            cohort: list[tuple[StageRef, StorageOperationRef]] = []
            for index in range(63):
                spec = stage_spec(identity, f"stage-{index}", b"")
                begin = operation(identity, f"begin-{index}", "begin_stage", spec)
                stage = await provider.begin_stage(begin, spec, context)
                cohort.append((stage, begin))
            high = await seal_repeated(provider, "high-slot", 4, context)
            private_slot = root / "stages" / "63"
            high_slot_materialized = bool(tuple(private_slot.iterdir()))
            absent = stage_spec(identity, absent_stage_id, b"")
            extra_begin = operation(identity, "absent-begin", "begin_stage", absent)
            absent_retire = operation(
                identity, "absent-retire", "retire_stage", absent.stage
            )

            with pytest.raises(KoraProtocolError) as full:
                await provider.begin_stage(extra_begin, absent, context)
            with pytest.raises(KoraProtocolError) as full_retire:
                await provider.retire_stage(absent_retire, absent.stage, context)
            acknowledgment = await acknowledge_stage(
                provider, high.content.stage, high.operations, context
            )
            retired_slot_present = private_slot.exists()
            original_stage = await provider.observe(high.operations[0], context)
            original_seal = await provider.observe(high.operations[-1], context)
            wrong = acknowledgment.model_copy(
                update={"mutation_set_digest": "sha256:" + "0" * 64}
            )
            with pytest.raises(KoraProtocolError) as wrong_acknowledgment:
                await provider.release_stage(wrong, context)
            charged_slots = provider.resource_counts()["stage_rows"]
            released = await provider.release_stage(acknowledgment, context)
            compacted = await provider.observe(high.operations[0], context)
            with pytest.raises(KoraProtocolError) as old_begin:
                await provider.begin_stage(extra_begin, absent, context)
            with pytest.raises(KoraProtocolError) as old_retire:
                await provider.retire_stage(absent_retire, absent.stage, context)

            assert full.value.code == full_retire.value.code == "storage_capacity"
            assert wrong_acknowledgment.value.code == "storage_conflict"
            assert charged_slots == 64
            assert high_slot_materialized
            assert not retired_slot_present
            assert original_stage.result == high.content.stage
            assert original_seal.result == high.content
            assert released.state == "released"
            assert compacted.state == "evidence_unavailable"
            assert (
                old_begin.value.code == old_retire.value.code == "storage_issuer_closed"
            )
        finally:
            await close_provider(provider)

        current = InspectedStorage(configuration)
        try:
            recovered = await current.retire_stage(absent_retire, absent.stage, context)
            observed = await current.observe(absent_retire, context)
            absent_acknowledgment = stage_release(
                absent.stage,
                ((absent_retire, recovered),),
                persisted_outcome_revision="absent-outcome",
                issuance_closed="absent-issuance-closed",
            )
            absent_released = await current.release_stage(
                absent_acknowledgment, context
            )
            cohort_releases: list[str] = []
            for stage, begin in cohort:
                settled = await acknowledge_stage(current, stage, (begin,), context)
                outcome = await current.release_stage(settled, context)
                cohort_releases.append(outcome.state)
            counts = current.resource_counts()

            assert current.identity() == identity
            assert recovered.completed
            assert observed.state == "settled"
            assert observed.effects_closed
            assert observed.result == recovered
            assert absent_released.state == "released"
            assert cohort_releases == ["released"] * len(cohort)
            assert all(count == 0 for count in counts.values())
            assert tuple((root / "stages").iterdir()) == ()
        finally:
            await close_provider(current)

        reopened = InspectedStorage(configuration)
        try:
            inventory = await reopened.scan("stages", None, 128, context)

            assert reopened.identity() == identity
            assert inventory.entries == ()
            assert inventory.next_cursor is None
        finally:
            await close_provider(reopened)

    asyncio.run(scenario())
    records = kora_capture.finish()

    refusals = [
        (record["operation"], record["reason"])
        for record in records
        if record["event"] == "storage.local.refused"
    ]
    assert refusals == [
        ("begin_stage", "storage_capacity"),
        ("retire_stage", "storage_capacity"),
        ("release_stage", "storage_conflict"),
        ("begin_stage", "storage_issuer_closed"),
        ("retire_stage", "storage_issuer_closed"),
    ]
    rendered = str(records)
    assert str(root) not in rendered
    assert absent_stage_id not in rendered
    assert "sha256:" not in rendered


@pytest.mark.parametrize("cancellations", [1, 2])
def test_cancelled_native_result_closes_transferred_lease(
    tmp_path: Path, cancellations: int
) -> None:
    import asyncio
    import threading

    from kora.plugins.storage.files import FileLease, RootAnchor

    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
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


def test_local_storage_seal_replay_refuses_a_different_stage(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        sealed, manifest, seal = await seal_bytes(
            provider, "sealed", b"immutable-data", context
        )
        other_stage = sealed.stage.model_copy(update={"stage_id": "different-stage"})

        try:
            with pytest.raises(KoraProtocolError) as refused:
                await provider.seal_stage(seal, other_stage, manifest, context)
            replayed = await provider.seal_stage(seal, sealed.stage, manifest, context)
        finally:
            await provider.aclose()

        assert refused.value.code == "storage_conflict"
        assert replayed == sealed

    asyncio.run(scenario())


def test_local_storage_retirement_cleans_failed_parts_and_refuses_reads(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    from kora.protocols.storage_models import NoEffectFailure, StageWrite

    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        payload = b"retired part"
        sealed, _, _ = await seal_bytes(provider, "sealed", payload, context)
        failed_spec = stage_spec(provider.identity(), "failed", payload)
        failed_stage = await provider.begin_stage(
            operation(provider.identity(), "failed-begin", "begin_stage", failed_spec),
            failed_spec,
            context,
        )
        part_spec = single_part(payload)
        failed_write = operation(
            provider.identity(),
            "failed-write",
            "write_part",
            StageWrite(version=1, stage=failed_stage, spec=part_spec),
        )

        try:
            retired = await provider.retire_stage(
                operation(provider.identity(), "retire", "retire_stage", sealed.stage),
                sealed.stage,
                context,
            )
            with pytest.raises(KoraProtocolError) as retired_read:
                await provider.read_stage(sealed, context)
            with pytest.raises(KoraProtocolError) as changed_bytes:
                await provider.write_part(
                    failed_write, failed_stage, part_spec, BytesSource(b"bad"), context
                )
            cleaned = await provider.retire_stage(
                operation(
                    provider.identity(), "failed-retire", "retire_stage", failed_stage
                ),
                failed_stage,
                context,
            )
            failure = await provider.observe(failed_write, context)
        finally:
            await provider.aclose()

        assert retired.completed
        assert retired_read.value.code == "storage_unavailable"
        assert changed_bytes.value.code == "storage_conflict"
        assert cleaned.completed
        assert failure.state == "settled"
        assert isinstance(failure.result, NoEffectFailure)
        assert list((tmp_path / "store" / "stages").iterdir()) == []

    asyncio.run(scenario())


def test_local_storage_refuses_replaced_control_inode(tmp_path: Path) -> None:
    import asyncio
    import shutil

    configuration = local_configuration(tmp_path / "store")
    provider = LocalArtifactStorage(configuration)
    asyncio.run(provider.aclose())
    database = tmp_path / "store" / "control.sqlite"
    original = tmp_path / "original.sqlite"
    database.rename(original)
    shutil.copyfile(original, database)
    database.chmod(0o600)

    with pytest.raises(KoraProtocolError) as replaced:
        LocalArtifactStorage(configuration)

    assert replaced.value.code == "storage_identity"


def test_local_storage_cancelled_reader_handoff_closes_undelivered_stream(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio
    from dataclasses import replace

    from tests.plugins.storage.activity import ReaderHandoffActivity

    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        activity = ReaderHandoffActivity()
        context = replace(context_for(kora_capture), activity=activity)
        payload = b"handoff"
        stage, _, manifest = await write_single_part(
            provider, "stage", payload, context, begin_id="begin", part_id="part"
        )
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

    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        spec = stage_spec(provider.identity(), "prefix", b"abc")
        part_spec = single_part(b"abc")
        arguments = PrefixCapture(
            version=1, stage=spec.stage, source_spec=part_spec, count=1
        )
        request = operation(provider.identity(), "prefix", "capture_prefix", arguments)
        source = BytesSource(b"abc")

        try:
            with pytest.raises(KoraProtocolError) as refused:
                await provider.capture_prefix(
                    request, spec.stage, part_spec, count, source, context
                )
        finally:
            await provider.aclose()

        assert refused.value.code == "storage_conflict"
        assert source.closed
        assert source.calls == 0

    asyncio.run(scenario())


def test_closed_receiver_closes_an_unconsumed_input(
    tmp_path: Path, kora_capture: Capture
) -> None:
    import asyncio

    from kora.protocols.storage_models import StageWrite

    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
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

        with pytest.raises(KoraProtocolError) as refused:
            await provider.write_part(write, spec.stage, part_spec, source, context)

        assert refused.value.code == "storage_closed"
        assert source.closed
        assert source.calls == 0

    asyncio.run(scenario())
