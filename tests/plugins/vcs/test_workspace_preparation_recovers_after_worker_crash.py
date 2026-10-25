from pathlib import Path

from kora.conformance.capture import Capture
from tests.plugins.vcs.git_support import make_world


def test_workspace_preparation_recovers_after_worker_crash(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)

    first = world.checkout()
    repeated = world.checkout()

    assert first.result == repeated.result
    assert first.areas.candidate != repeated.areas.candidate
