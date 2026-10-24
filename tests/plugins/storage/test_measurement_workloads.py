import asyncio
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.storage_models import StorageLimits
from tests.measurements.storage_lifetime_manifest import SMALL_ENDINGS, Workload
from tests.measurements.storage_lifetime_resources import disk_counts
from tests.measurements.storage_lifetime_workload import cycle


@pytest.mark.parametrize("ending", SMALL_ENDINGS)
@pytest.mark.parametrize("size", [0, 1, 65536])
def test_measured_storage_cycle_closes_actual_resources(
    tmp_path: Path, kora_capture: Capture, ending: str, size: int
) -> None:
    configuration = LocalStorageConfiguration(
        root=str(tmp_path / "store"), limits=StorageLimits(version=1)
    )
    context = context_for(kora_capture)

    counts, _ = asyncio.run(
        cycle(configuration, Workload(ending, size, 1), context, "cycle", lambda: None)
    )
    physical = disk_counts(tmp_path / "store")

    assert counts["receipts"] == 0
    assert counts["reserved_bytes"] == 0
    assert physical["private_files"] == 0
    assert physical["payload_links"] == 0
