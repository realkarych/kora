import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIOS: tuple[str, ...] = (
    (
        "readable",
        "temporary-denial",
        "persistent-denial",
        "root-exit",
        "late-snapshot",
    )
    if sys.platform == "linux"
    else ("readable", "late-snapshot")
)
ROOT: Path = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_owner_admits_complete_visibility_with_shared_close_deadline(
    scenario: str,
) -> None:
    node = shutil.which("node")
    assert node is not None
    command = [
        node,
        str(ROOT / "web/tests/browser/ownership-probe.ts"),
        scenario,
        sys.executable,
        str(Path(__file__).with_name("held_process.py")),
    ]

    result = subprocess.run(command, capture_output=True, timeout=20, check=False)

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == f"{scenario}\n".encode()
