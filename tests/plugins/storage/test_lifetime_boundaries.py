import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.storage_models import StorageLimits
from tests.measurements.harness import Report, Sample
from tests.measurements.storage_lifetime_manifest import Workload
from tests.measurements.storage_lifetime_resources import process_ownership
from tests.measurements.storage_lifetime_workload import cycle
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.lifetime_boundaries import (
    bounded_reconstruction,
    inventory_workload,
    maximum_slots,
    receiver_churn,
)


@pytest.mark.parametrize("count", [32, 256, 2048])
def test_local_storage_inventory_query_work(
    tmp_path: Path, kora_capture: Capture, boundary_report: Report, count: int
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )

    asyncio.run(
        inventory_workload(
            configuration, count, context_for(kora_capture), boundary_report
        )
    )

    assert boundary_report.gates[f"storage.inventory.{count}"]


def test_local_storage_receiver_churn_is_bounded(
    tmp_path: Path, kora_capture: Capture, boundary_report: Report
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )

    asyncio.run(
        receiver_churn(configuration, context_for(kora_capture), boundary_report)
    )

    assert boundary_report.gates["storage.receiver_churn"]


def test_local_storage_control_progress_under_saturation(
    tmp_path: Path, kora_capture: Capture, boundary_report: Report
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"),
        limits=StorageLimits(
            version=1,
            stage_slots=32,
            deletion_slots=32,
            data_work_slots=8,
            reader_slots=16,
        ),
    )

    asyncio.run(
        maximum_slots(configuration, context_for(kora_capture), boundary_report)
    )

    assert boundary_report.gates["storage.maximum_slots"]


def test_three_epoch_changes_return_pending_before_a_later_request_progresses(
    tmp_path: Path, kora_capture: Capture, boundary_report: Report
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )

    asyncio.run(
        bounded_reconstruction(
            configuration, context_for(kora_capture), boundary_report
        )
    )

    assert boundary_report.gates["storage.bounded_reconstruction"]


@pytest.mark.parametrize("count", [0, 1, 65536, 16777215])
def test_local_storage_prefix_boundaries(
    tmp_path: Path, kora_capture: Capture, boundary_report: Report, count: int
) -> None:
    import time

    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )
    context = context_for(kora_capture)
    repeats = 3 if count == 16777215 else 1

    async def scenario() -> None:
        if repeats == 3:
            await cycle(
                configuration,
                Workload("prefix", count, repeats),
                context,
                "prefix-warmup",
                lambda: None,
            )
        for repeat in range(repeats):
            started = time.perf_counter()
            counts, observer = await cycle(
                configuration,
                Workload("prefix", count, repeats),
                context,
                f"prefix-{repeat}",
                lambda: None,
            )
            elapsed = time.perf_counter() - started - observer
            boundary_report.samples.append(
                Sample(
                    "storage.prefix", count, 1, elapsed, {"cycle": repeat + 1, **counts}
                )
            )
        boundary_report.gates[f"storage.prefix.{count}"] = True

    asyncio.run(scenario())

    assert boundary_report.gates[f"storage.prefix.{count}"]


@pytest.mark.parametrize(
    "workload", ["slots", "churn", "reconstruction", "reconstruction_exit"]
)
def test_boundary_setup_cancellation_closes_actual_ownership(
    tmp_path: Path, kora_capture: Capture, workload: str
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"),
        limits=StorageLimits(
            version=1,
            stage_slots=32,
            deletion_slots=32,
            data_work_slots=8,
            reader_slots=16,
        ),
    )
    barrier = StorageBarrierActivity(
        "storage.boundary.reconstruction.peer_acquisition"
        if workload.startswith("reconstruction")
        else "storage.begin_stage.admitted",
        on_exit=workload == "reconstruction_exit",
    )
    context = replace(context_for(kora_capture), activity=barrier)

    async def scenario() -> None:
        from tests.measurements.harness import new_report

        before = process_ownership()
        selected = {
            "slots": maximum_slots,
            "churn": receiver_churn,
            "reconstruction": bounded_reconstruction,
            "reconstruction_exit": bounded_reconstruction,
        }[workload]
        pending = asyncio.create_task(selected(configuration, context, new_report()))
        try:
            await asyncio.wait_for(barrier.entered.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            barrier.released.set()

        assert process_ownership() == before

    asyncio.run(scenario())
