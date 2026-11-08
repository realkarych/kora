import json
import shutil
from pathlib import Path
from typing import NoReturn

import pytest

from kora.protocols.secret_value import SecretScope
from tests.plugins.harness.codex.probe import ProbeFailure, selected_launch_workspace
from tests.plugins.harness.codex.selected_bound_environment import BoundEnvironmentProbe


def fail_with_native_state(root: Path) -> NoReturn:
    (root / "native-state").write_text("fixture", encoding="utf-8")
    raise ProbeFailure("original_failure")


def fail_after_workspace_disappears(root: Path) -> NoReturn:
    root.rmdir()
    raise ProbeFailure("original_failure")


def test_unsettled_bound_workspace_stays_with_the_container() -> None:
    bound = BoundEnvironmentProbe("ready", SecretScope(None), SecretScope(None))
    root: Path | None = None

    try:
        with (
            pytest.raises(ProbeFailure, match="^original_failure$"),
            selected_launch_workspace(bound) as root,
        ):
            fail_with_native_state(root)

        assert (root / "native-state").read_text(encoding="utf-8") == "fixture"
    finally:
        if root is not None:
            shutil.rmtree(root)


def test_settled_bound_workspace_cleanup_preserves_original_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bound = BoundEnvironmentProbe("ready", SecretScope(None), SecretScope(None))
    bound.settled = True

    with (
        pytest.raises(ProbeFailure, match="^original_failure$"),
        selected_launch_workspace(bound) as root,
    ):
        fail_after_workspace_disappears(root)

    assert json.loads(capsys.readouterr().err) == {
        "level": "ERROR",
        "event": "probe.selected_bound_workspace_cleanup_failed",
        "detail": "ready",
    }
