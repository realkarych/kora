import sys
from pathlib import Path

import pytest

from tests.plugins.storage.pressure_controller import create, read_manifest
from tests.plugins.storage.pressure_models import PressureFailure
from tests.plugins.storage.pressure_native import process_token


def test_terminated_command_retains_uncertainty_after_actual_process_death(
    tmp_path: Path,
) -> None:
    with create(tmp_path / "terminated-command") as controller:
        arguments = (
            sys.executable,
            "-I",
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        )

        with pytest.raises(PressureFailure, match="command_terminated"):
            controller.effect(arguments)
        with pytest.raises(PressureFailure, match="acquisition_unresolved"):
            controller.cleanup()
        saved = read_manifest(controller.directory / "pressure-manifest.json")

        assert saved.command is not None
        assert process_token(saved.command.pid) is None
        assert saved.command_inflight
        assert saved.command_exit_status is None
        assert not saved.resources_closed
