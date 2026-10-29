import select
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIOS: tuple[str, ...] = (
    (
        "released",
        "persistent-live",
        "temporary-denial",
        "persistent-denial",
        "temporary-admission",
        "persistent-admission",
    )
    if sys.platform == "linux"
    else ("released", "persistent-live")
)
ROOT: Path = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_release_wait_observes_real_process_lifetime(scenario: str) -> None:
    node = shutil.which("node")
    assert node is not None
    visibility = "denied" if scenario.endswith(("denial", "admission")) else "readable"
    command = [sys.executable, str(Path(__file__).with_name("held_process.py"))]
    with subprocess.Popen(
        [*command, visibility],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as held:
        try:
            assert held.stdout is not None
            assert held.stdin is not None
            assert select.select([held.stdout], [], [], 10)[0]
            assert held.stdout.readline() == b"ready\n"

            result = subprocess.run(
                [
                    node,
                    str(ROOT / "web/tests/browser/resource-probe.ts"),
                    scenario,
                    str(held.pid),
                    str(held.stdin.fileno()),
                    str(held.stdout.fileno()),
                ],
                pass_fds=(held.stdin.fileno(), held.stdout.fileno()),
                capture_output=True,
                timeout=15,
                check=False,
            )

            assert result.returncode == 0, result.stderr.decode("utf-8")
            assert result.stdout == f"{scenario}\n".encode()
            if scenario.startswith("persistent"):
                assert held.poll() is None
            else:
                assert held.wait(timeout=10) == -signal.SIGTERM
        finally:
            if held.poll() is None:
                held.kill()
            held.wait(timeout=10)
