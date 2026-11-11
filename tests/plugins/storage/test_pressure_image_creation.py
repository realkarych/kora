import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from tests.plugins.storage.pressure_controller import create
from tests.plugins.storage.pressure_images import (
    create_image,
    discover,
    image_name,
)
from tests.plugins.storage.pressure_models import PressureFailure
from tests.plugins.storage.pressure_native import collect_process, command

REPOSITORY: Path = Path(__file__).resolve().parents[3]


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


def close_input(process: subprocess.Popen[bytes]) -> None:
    assert process.stdin is not None
    try:
        process.stdin.close()
    except BrokenPipeError:
        assert process.stdin.closed


def stop_dissenter(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes, int]:
    try:
        close_input(process)
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


def mount_flags(mountpoint: Path) -> frozenset[bytes]:
    anchor = b" on " + os.fsencode(mountpoint) + b" ("
    listed = tuple(
        line for line in command(("/sbin/mount",)).splitlines() if anchor in line
    )
    assert len(listed) == 1
    return frozenset(listed[0].rsplit(b" (", 1)[1].removesuffix(b")").split(b", "))


def test_image_acquisition_requests_no_ejection(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        assert_dissenter_refuses_platform(tmp_path / image_name())
        return
    with create(tmp_path / "guarded-acquisition") as controller:
        dissenter = start_dissenter(controller.directory / image_name())
        cleanup_started = False
        try:
            assert next_line(dissenter) == b"ready\n"
            controller.acquire("enospc")
            stamp = controller.manifest.image_stamp
            assert stamp is not None
            attached = discover(controller.directory, stamp)
            flags = mount_flags(controller.directory / "mounted")
            observing = observe_ejections(dissenter)
            cleanup_started = True
            controller.cleanup()
        finally:
            try:
                output, error_output, status = stop_dissenter(dissenter)
            finally:
                if not cleanup_started:
                    controller.cleanup()

    assert observing == b"observing\n"
    assert status == 0, error_output
    assert output.splitlines()
    assert set(output.splitlines()) == {b"observed"}
    assert attached is not None
    assert attached.mounted
    assert controller.report.filesystem == "hfs"
    assert {b"hfs", b"nodev", b"nosuid", b"nobrowse"} <= flags
    assert b"noowners" not in flags
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
