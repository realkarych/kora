import inspect
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from kora.conformance.common import kora_capture
from kora.plugins.storage.local import LocalArtifactStorage
from tests.measurements.harness import Report, new_report
from tests.measurements.storage_lifetime_resources import source_digest
from tests.plugins.storage.pressure_native import command

__all__ = ["kora_capture"]


@pytest.fixture(scope="session")
def boundary_report(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Report]:
    root = Path(__file__).resolve().parents[3]
    report = new_report()
    report.revision = command(("git", "rev-parse", "HEAD")).decode().strip()
    report.environment.update(
        source_digest=source_digest(root),
        checkout_clean=not bool(command(("git", "status", "--porcelain"))),
        core_authority="provider_harness_counters_not_durable_core_queries",
        installed_source=str(
            Path(inspect.getfile(LocalArtifactStorage)).resolve().relative_to(root)
        ),
        load_context=os.environ.get("KORA_STORAGE_LOAD_CONTEXT", "local_unspecified"),
    )
    destination = os.environ.get("KORA_STORAGE_BOUNDARY_OUTPUT")
    output = (
        Path(destination)
        if destination
        else tmp_path_factory.mktemp("boundary-evidence") / "boundaries.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        yield report
    finally:
        report.durations["complete"] = time.perf_counter() - started
        output.write_text(report.to_json(), encoding="utf-8")
