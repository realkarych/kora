import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.ir.json import JsonObject
from tests.measurements.command_measurement import publish_command
from tests.measurements.lifetime_cohorts import Cohort


def test_command_evidence_preserves_later_cohorts_and_original_failure(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "command-artifacts"
    folder.mkdir(mode=0o700)
    cohorts: list[Cohort] = []
    result: JsonObject = {"all_270_cycles": False}
    started = time.monotonic()
    with TemporaryDirectory(prefix="kora-evidence-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        for index in range(3):
            parent = folder / str(index)
            parent.mkdir(mode=0o700)
            home = root / f"kora-life-{index:08}"
            home.mkdir(mode=0o700)
            (parent / "home.txt").write_text(str(home))
            cohorts.append(Cohort((), home, parent))
        (cohorts[0].folder / "home.txt").unlink()
        (cohorts[1].home / "journal").write_text("retained")
        cohorts[2].home.rmdir()

        def failing_workload() -> None:
            try:
                raise ValueError("original workload failure")
            finally:
                publish_command(
                    result,
                    tmp_path / "report.json",
                    cohorts,
                    {2},
                    started,
                    started + 30,
                    failed=True,
                )

        with pytest.raises(ValueError, match="original workload failure"):
            failing_workload()

        publication = json.loads((folder / "publication.json").read_text())
        sidecar = json.loads((tmp_path / "command.json").read_text())
        assert [item["state"] for item in publication["items"]] == [
            "unverified",
            "retained",
            "removed",
        ]
        assert (folder / "retained-1.tar").is_file()
        assert (cohorts[1].home / "journal").read_text() == "retained"
        assert sidecar["retention_verified"] is False
        assert sidecar["all_270_cycles"] is False
