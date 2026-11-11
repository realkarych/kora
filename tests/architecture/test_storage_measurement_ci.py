import subprocess
import sys
from pathlib import Path

from tests.measurements.storage_qualification_evidence import qualification_nodes
from tests.scaffold_evidence import STORAGE_ARGUMENTS, collection_nodes

ROOT = Path(__file__).resolve().parents[2]


def test_storage_qualification_cases_remain_in_ordinary_collection(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "storage.collection"
    arguments = [sys.executable, "-m", "pytest", "--collect-only", "-q"]

    result = subprocess.run(
        [*arguments, *STORAGE_ARGUMENTS],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    collection.write_bytes(result.stdout)

    assert result.returncode == 0, result.stderr.decode()
    required = qualification_nodes()
    collected = collection_nodes(collection)
    pressure = "tests.plugins.storage.test_pressure_controller."
    required_pressure = frozenset(
        node for node in required if node.startswith(pressure)
    )
    collected_pressure = frozenset(
        node for node in collected if node.startswith(pressure)
    )

    assert required <= collected
    assert required_pressure == collected_pressure
