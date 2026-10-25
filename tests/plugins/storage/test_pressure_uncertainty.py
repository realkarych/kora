import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.plugins.storage.pressure_controller import create, read_manifest
from tests.plugins.storage.pressure_models import PressureFailure
from tests.plugins.storage.pressure_native import process_token


def test_exited_unreaped_command_preserves_its_native_identity() -> None:
    process = subprocess.Popen(
        ("/usr/bin/false",), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        deadline = time.monotonic() + 10
        for stream in (process.stdout, process.stderr):
            readable, _, _ = select.select(
                [stream], [], [], max(0, deadline - time.monotonic())
            )
            assert readable
            assert stream.read(1) == b""
        while process_token(process.pid) is not None:
            assert time.monotonic() < deadline

        token = process_token(process.pid, include_exited=True)
    finally:
        if process.poll() is None:
            process.kill()
        status = process.wait(timeout=10)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    assert token is not None
    assert token.pid == process.pid
    assert status == 1
    assert process_token(process.pid, include_exited=True) is None


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
