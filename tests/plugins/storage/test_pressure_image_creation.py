import os
import plistlib
import select
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from tests.plugins.storage.pressure_controller import PressureController, create
from tests.plugins.storage.pressure_images import (
    create_image,
    discover,
    image_name,
)
from tests.plugins.storage.pressure_models import PressureFailure
from tests.plugins.storage.pressure_native import collect_process, command

REPOSITORY: Path = Path(__file__).resolve().parents[3]


class NativeImageInformation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    format: StrictStr = Field(alias="Format")


def start_dissenter(image: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.plugins.storage.eject_dissenter", str(image)],
        cwd=REPOSITORY,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def next_line(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], 30)
    assert readable
    return process.stdout.readline(64)


def observe_ejections(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdin is not None
    process.stdin.write(b"o")
    process.stdin.flush()
    return next_line(process)


def stop_dissenter(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes, int]:
    try:
        assert process.stdin is not None
        process.stdin.close()
        return collect_process(process, 30)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def assert_dissenter_refuses_platform(image: Path) -> None:
    output, error_output, status = stop_dissenter(start_dissenter(image))

    assert status != 0
    assert output == b""
    assert b"native_platform" in error_output


def image_format(controller: PressureController) -> str:
    raw: object = plistlib.loads(
        command(
            (
                "/usr/bin/hdiutil",
                "imageinfo",
                "-plist",
                str(controller.directory / image_name()),
            )
        )
    )
    return NativeImageInformation.model_validate(raw).format


def test_image_acquisition_requests_no_ejection(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        assert_dissenter_refuses_platform(tmp_path / image_name())
        return
    with create(tmp_path / "guarded-acquisition") as controller:
        dissenter = start_dissenter(controller.directory / image_name())
        try:
            ready = next_line(dissenter)
            controller.acquire("enospc")
            stamp = controller.manifest.image_stamp
            assert stamp is not None
            attached = discover(controller.directory, stamp)
            native_format = image_format(controller)
        finally:
            try:
                observing = observe_ejections(dissenter)
                controller.cleanup()
            finally:
                output, error_output, status = stop_dissenter(dissenter)

    assert ready == b"ready\n"
    assert observing == b"observing\n"
    assert status == 0, error_output
    assert output.splitlines()
    assert set(output.splitlines()) == {b"observed"}
    assert attached is not None
    assert attached.mounted
    assert controller.report.filesystem == "hfs"
    assert native_format == "UDRW"
    assert controller.manifest.resources_closed
    assert not (controller.directory / "mounted").exists()
    assert not (controller.directory / image_name()).exists()


def test_image_creation_failure_fails_after_one_command(tmp_path: Path) -> None:
    observed: list[tuple[str, ...]] = []
    with create(tmp_path / "existing-image") as controller:
        controller.manifest = controller.manifest.model_copy(
            update={"image_requested": True}
        )
        controller.persist()
        image = controller.directory / image_name()
        os.close(os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

        def effect(arguments: tuple[str, ...]) -> bytes:
            observed.append(arguments)
            return controller.effect(arguments)

        try:
            with pytest.raises((PressureFailure, FileExistsError)) as failure:
                create_image(controller.directory, controller.manifest.nonce, effect)
        finally:
            controller.cleanup()

    assert controller.manifest.resources_closed
    assert not image.exists()
    if sys.platform == "darwin":
        assert isinstance(failure.value, PressureFailure)
        assert failure.value.code == "command_failed"
        assert failure.value.command_stderr is not None
        assert failure.value.command_stderr.splitlines()[-1] == (
            b"hdiutil: create failed - File exists"
        )
        assert observed == [
            (
                "/usr/bin/hdiutil",
                "create",
                "-size",
                "64m",
                "-layout",
                "GPTSPUD",
                "-o",
                str(image),
            )
        ]
    else:
        assert isinstance(failure.value, FileExistsError)
        assert observed == []
