import json
import os
import shutil
from pathlib import Path
from typing import NoReturn

import pytest

from kora.protocols.secret_value import SecretScope
from tests.plugins.harness.codex.probe import ProbeFailure, selected_launch_workspace
from tests.plugins.harness.codex.selected_bound_environment import (
    BoundEnvironmentError,
    BoundEnvironmentProbe,
)
from tests.plugins.harness.codex.selected_launch import prepare_native_inputs
from tests.plugins.runtime.local.frames_support import ORIGIN_NS, StepClock


def fail_with_native_state(root: Path) -> NoReturn:
    (root / "native-state").write_text("fixture", encoding="utf-8")
    raise ProbeFailure("original_failure")


def fail_after_workspace_disappears(root: Path) -> NoReturn:
    root.rmdir()
    raise ProbeFailure("original_failure")


def test_unsettled_bound_workspace_stays_with_the_container() -> None:
    bound = BoundEnvironmentProbe(
        "ready", SecretScope(None), SecretScope(None), SecretScope(None)
    )
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
    bound = BoundEnvironmentProbe(
        "ready", SecretScope(None), SecretScope(None), SecretScope(None)
    )
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


def test_configuration_workspace_refuses_to_remove_replacement(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "scratch"
    root.mkdir(mode=0o700)
    moved = root.with_name("original-scratch")
    inputs = prepare_native_inputs(
        root, root / "workspace", binary=Path("/opt/codex"), port=1234, bearer="fixture"
    )
    bound = BoundEnvironmentProbe(
        "configuration-ready", SecretScope(None), SecretScope(None), SecretScope(None)
    )
    baseline = set(os.listdir("/dev/fd"))
    bound.prepare_configurations(root, inputs, ORIGIN_NS + 100, StepClock())
    bound.close_inputs()
    root.rename(moved)
    root.mkdir(mode=0o700)

    with pytest.raises(
        BoundEnvironmentError, match="^selected_bound_workspace_identity$"
    ):
        bound.remove_workspace(root)

    assert root.is_dir()
    assert (moved / "app" / "codex" / "config.toml").is_file()
    assert set(os.listdir("/dev/fd")) == baseline
