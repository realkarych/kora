import errno
import os
import plistlib
import select
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from tests.measurements.storage_pressure_evidence import preserve_owner
from tests.plugins.storage.pressure_models import PressureFailure, PressureReport
from tests.plugins.storage.pressure_native import (
    collect_process,
    command,
    process_token,
)


@pytest.mark.parametrize("repetitions", [1, 4096])
def test_closed_failed_command_does_not_block_pressure_recovery(
    tmp_path: Path, repetitions: int
) -> None:
    from tests.plugins.storage.pressure_controller import create

    directory = tmp_path / "failed-command"
    output = tmp_path / "evidence"
    output.mkdir()
    fragment = b"fixture-command-error\xff\r\n\x1b"
    diagnostic = fragment * repetitions
    arguments = (
        sys.executable,
        "-I",
        "-c",
        f"import os; os.write(2, {fragment!r} * {repetitions}); raise SystemExit(17)",
    )

    with create(directory) as controller:
        with pytest.raises(PressureFailure, match="command_failed") as failure:
            controller.effect(arguments)
        pending = directory / "command-stderr.pending"
        pending.symlink_to(directory / "absent")
        try:
            with pytest.raises(BaseExceptionGroup) as persistence:
                controller.effect(arguments)
        finally:
            pending.unlink()
        controller.cleanup()
    preserve_owner(directory / "pressure-manifest.json", output, "failed")

    assert (directory / "command-stderr.txt").read_bytes() == diagnostic
    assert (output / "failed.command.stderr.txt").read_bytes() == diagnostic
    assert controller.manifest.command_exit_status == 17
    assert not controller.manifest.command_inflight
    assert controller.manifest.resources_closed
    assert controller.report.failure == "command_failed"
    rendered = str(failure.value)
    assert "python" in rendered
    assert "exit=17" in rendered
    assert "\\xff\\r\\n\\x1b" in rendered
    assert len(rendered.encode()) <= 16384
    assert all(32 <= ord(character) <= 126 for character in rendered)
    assert ("[truncated]" in rendered) == (repetitions == 4096)
    assert len(persistence.value.exceptions) == 2
    assert isinstance(persistence.value.exceptions[0], PressureFailure)
    assert persistence.value.exceptions[0].code == "command_failed"
    assert isinstance(persistence.value.exceptions[1], OSError)


def controller_arguments(directory: Path, workload: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "tests.plugins.storage.pressure_controller",
        "--directory",
        str(directory),
        "--workload",
        workload,
    ]


def read_report(directory: Path) -> PressureReport:
    report = PressureReport.model_validate_json(
        (directory / "report.json").read_bytes()
    )
    primary = directory / "primary-report.json"
    if not primary.exists():
        primary.write_text(report.model_dump_json(), encoding="utf-8")
    return report


def recover_owner(directory: Path) -> None:
    manifest = directory / "pressure-manifest.json"
    if manifest.exists():
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.plugins.storage.pressure_controller",
                "--recover",
                str(manifest),
            ],
            cwd=Path(__file__).resolve().parents[3],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def close_controller(process: subprocess.Popen[bytes], directory: Path) -> None:
    try:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        recover_owner(directory)


def controller_diagnostics(directory: Path) -> tuple[bytes | None, ...]:
    result: list[bytes | None] = []
    for name in ("report.json", "command-stderr.txt"):
        path = directory / name
        if path.exists():
            with path.open("rb") as source:
                result.append(source.read(16384))
        else:
            result.append(None)
    return tuple(result)


def run_controller(
    directory: Path, workload: str
) -> tuple[subprocess.CompletedProcess[bytes], PressureReport]:
    arguments = controller_arguments(directory, workload)
    process = subprocess.Popen(
        arguments,
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        output, error_output, status = collect_process(process, 120)
        return (
            subprocess.CompletedProcess(arguments, status, output, error_output),
            read_report(directory),
        )
    finally:
        close_controller(process, directory)


def test_pressure_controller_preserves_caught_native_failure(tmp_path: Path) -> None:
    from tests.plugins.storage.pressure_controller import read_manifest

    directory = tmp_path / "native-failure"

    result, report = run_controller(directory, "command-failure")
    manifest = read_manifest(directory / "pressure-manifest.json")
    diagnostic = (directory / "command-stderr.txt").read_bytes()

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.endswith(b"\n")
    assert len(result.stderr) <= 16385
    assert all(32 <= byte <= 126 for byte in result.stderr[:-1])
    assert b"python" in result.stderr
    assert b"exit=17" in result.stderr
    assert b"\\xff\\r\\n\\x1b" in result.stderr
    assert b"[truncated]" in result.stderr
    assert diagnostic == b"pressure-native\xff\r\n\x1b" * 4096
    assert report.failure == "command_failed"
    assert report.resources_closed
    assert manifest.command_exit_status == 17
    assert not manifest.command_inflight
    assert manifest.child is not None
    assert process_token(manifest.child.pid) is None


def test_pressure_controller_closes_before_stopped_diagnostic_reader(
    tmp_path: Path,
) -> None:
    from tests.plugins.storage.pressure_controller import acquire_lease, read_manifest

    directory = tmp_path / "blocked-diagnostic"
    reader, writer = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        os.set_blocking(writer, False)
        filled = 0
        while filled < 1048576:
            try:
                filled += os.write(writer, b"x" * 4096)
            except BlockingIOError:
                break
        assert filled < 1048576
        os.set_blocking(writer, True)

        process = subprocess.Popen(
            controller_arguments(directory, "command-failure"),
            cwd=Path(__file__).resolve().parents[3],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=writer,
        )
        deadline = time.monotonic() + 30
        while not (directory / "report.json").exists():
            assert time.monotonic() < deadline
        report = read_report(directory)
        manifest = read_manifest(directory / "pressure-manifest.json")
        while True:
            try:
                lease = acquire_lease(directory, manifest.lease_stamp)
            except PressureFailure as error:
                if error.code != "controller_busy":
                    raise
                assert time.monotonic() < deadline
            else:
                lease.close()
                break
        retained_process = process.poll()
    finally:
        try:
            if process is not None:
                close_controller(process, directory)
        finally:
            os.close(writer)
            os.close(reader)

    assert retained_process is None
    assert report.failure == "command_failed"
    assert report.resources_closed
    assert manifest.resources_closed
    assert manifest.child is not None
    assert process_token(manifest.child.pid) is None


class NativeImageInformation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    format: StrictStr = Field(alias="Format")


def test_pressure_image_creation_keeps_native_format_and_private_identity(
    tmp_path: Path,
) -> None:
    from tests.plugins.storage import pressure_images
    from tests.plugins.storage.pressure_controller import create

    observed: list[tuple[str, ...]] = []
    with create(tmp_path / "image-creation") as controller:
        controller.manifest = controller.manifest.model_copy(
            update={"image_requested": True}
        )
        controller.persist()

        def effect(arguments: tuple[str, ...]) -> bytes:
            observed.append(arguments)
            return controller.effect(arguments)

        try:
            stamp = pressure_images.create_image(
                controller.directory, controller.manifest.nonce, effect
            )
            image = controller.directory / pressure_images.image_name()
            actual = pressure_images.inspect_image(image, None)
            information = image.stat()
            if sys.platform == "darwin":
                raw: object = plistlib.loads(
                    command(("/usr/bin/hdiutil", "imageinfo", "-plist", str(image)))
                )
                image_format = NativeImageInformation.model_validate(raw).format
            else:
                image_format = "ext4"
            attached = pressure_images.discover(controller.directory, actual)
        finally:
            controller.cleanup()

    assert stamp == actual
    assert information.st_uid == os.geteuid()
    assert information.st_mode & 0o777 == 0o600
    assert 0 < information.st_size <= 68157440
    assert information.st_nlink == 1
    assert attached is None
    assert controller.manifest.resources_closed
    if sys.platform == "darwin":
        assert observed[0][:7] == (
            "/usr/bin/hdiutil",
            "create",
            "-size",
            "64m",
            "-fs",
            "HFS+",
            "-volname",
        )
        assert "-srcfolder" not in observed[0]
        assert image_format == "UDRW"
    else:
        assert observed[0][0] == "/usr/sbin/mkfs.ext4"


def test_pressure_owner_recovers_legacy_source_directory(tmp_path: Path) -> None:
    from tests.plugins.storage.pressure_controller import read_manifest
    from tests.plugins.storage.pressure_images import image_name

    directory = tmp_path / "legacy-owner"
    process = subprocess.Popen(
        controller_arguments(directory, "parent-eof"),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 30)
        assert ready
        assert process.stdout.readline(1024) == b"provider_started\n", (
            controller_diagnostics(directory)
        )
        mounted = (directory / "mounted").stat()
        legacy = directory / "volume-root"
        legacy.mkdir(mode=0o700, exist_ok=True)
        manifest = read_manifest(directory / "pressure-manifest.json")
        process.kill()
        process.wait(timeout=10)
        recover_owner(directory)
        report = read_report(directory)
    finally:
        close_controller(process, directory)

    assert manifest.version == 1
    assert mounted.st_uid == os.geteuid()
    if sys.platform == "darwin":
        assert mounted.st_mode & 0o777 == 0o700
    assert report.resources_closed
    assert not (directory / "mounted").exists()
    assert not (directory / image_name()).exists()
    assert not legacy.exists()


@pytest.mark.parametrize(
    ("workload", "reason"),
    [
        ("parent-eof", "controller_control_closed"),
        ("cancelled", "controller_cancelled"),
        ("deadline", "child_deadline"),
    ],
)
def test_pressure_owner_closes_after_parent_interrupt(
    tmp_path: Path, workload: str, reason: str
) -> None:
    directory = tmp_path / "interrupted"
    process = subprocess.Popen(
        controller_arguments(directory, workload),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    try:
        readable, _, _ = select.select([process.stdout], [], [], 30)
        assert readable
        phase = process.stdout.readline(1024)
        assert phase == b"provider_started\n", controller_diagnostics(directory)
        if workload == "parent-eof":
            process.stdin.close()
        elif workload == "cancelled":
            process.stdin.write(b"c")
            process.stdin.flush()
        _, error_output, status = collect_process(process, 30)
        report = read_report(directory)
    finally:
        close_controller(process, directory)

    assert status != 0, error_output
    assert report.provider_started, error_output
    assert report.failure == reason
    assert report.resources_closed
    assert not (directory / "mounted").exists()


def test_pressure_owner_refuses_writable_ancestor_before_acquisition(
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    directory = unsafe / "pressure"

    result = subprocess.run(
        controller_arguments(directory, "partial-acquisition"),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert not (directory / "pressure-manifest.json").exists()
    assert not (directory / "filesystem.img").exists()
    assert not (directory / "filesystem.dmg").exists()


def test_pressure_owner_refuses_symlinked_acquisition_ancestor(tmp_path: Path) -> None:
    safe = tmp_path / "safe"
    safe.mkdir(mode=0o700)
    unsafe = tmp_path / "unsafe-alias"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    (unsafe / "alias").symlink_to(safe, target_is_directory=True)
    directory = unsafe / "alias" / "pressure"

    result = subprocess.run(
        controller_arguments(directory, "partial-acquisition"),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert not (safe / "pressure" / "pressure-manifest.json").exists()


def test_local_storage_enospc_recovers(tmp_path: Path) -> None:
    directory = tmp_path / "pressure"

    result, report = run_controller(directory, "enospc")

    assert result.returncode == 0, (result.stderr, report)
    assert report.provider_started, result.stderr
    assert report.payload_enospc
    assert report.metadata_enospc
    assert report.payload_errno == errno.ENOSPC
    assert report.metadata_headroom_bytes is not None
    assert 0 < report.metadata_headroom_bytes <= 1048576
    assert (
        report.metadata_errno == errno.ENOSPC
        or report.metadata_sqlite_code == sqlite3.SQLITE_FULL
    )
    assert report.unknown_state_charged
    assert report.cleanup_completed
    assert report.resources_closed
    assert not (
        directory / ("filesystem.dmg" if sys.platform == "darwin" else "filesystem.img")
    ).exists()


def test_storage_pressure_controller_closes_on_child_failure(tmp_path: Path) -> None:
    directory = tmp_path / "child-failure"

    result, report = run_controller(directory, "child-failure")

    assert result.returncode != 0
    assert report.provider_started, result.stderr
    assert report.failure == "child_failure", result.stderr
    assert report.resources_closed, result.stderr
    assert not (directory / "mounted").exists()


def test_storage_pressure_controller_recovers_partial_acquisition(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "partial"
    process = subprocess.Popen(
        controller_arguments(directory, "partial-acquisition"),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None

    try:
        readable, _, _ = select.select([process.stdout], [], [], 30)
        assert readable
        signal = process.stdout.readline(1024)
        assert signal == b"image_created\n"
        process.kill()
        process.wait(timeout=10)
        recovered = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.plugins.storage.pressure_controller",
                "--recover",
                str(directory / "pressure-manifest.json"),
            ],
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True,
            timeout=60,
            check=False,
        )
    finally:
        close_controller(process, directory)

    report = read_report(directory)
    assert recovered.returncode == 0, recovered.stderr
    assert report.resources_closed
    assert os.stat(directory).st_mode & 0o777 == 0o700


def test_pressure_owner_reconciles_lost_attachment_response(tmp_path: Path) -> None:
    directory = tmp_path / "lost-attachment"
    process = subprocess.Popen(
        controller_arguments(directory, "lost-attachment-response"),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 30)
        assert readable
        assert process.stdout.readline(1024) == b"attachment_created\n", (
            controller_diagnostics(directory)
        )
        from tests.plugins.storage.pressure_controller import read_manifest

        saved = read_manifest(directory / "pressure-manifest.json")
        assert saved.command_inflight
        assert saved.command is not None
        assert saved.command_exit_status == 0
        process.kill()
        process.wait(timeout=10)

        recover_owner(directory)
        report = read_report(directory)
    finally:
        close_controller(process, directory)

    assert report.resources_closed
    assert not (directory / "mounted").exists()


def test_failed_unmount_retains_ownership_until_the_blocker_closes(
    tmp_path: Path,
) -> None:
    from tests.plugins.storage.pressure_controller import create

    with create(tmp_path / "busy-mount") as controller:
        try:
            controller.acquire("enospc")
            blocker = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys; print('ready', flush=True); sys.stdin.buffer.read(1)",
                ],
                cwd=controller.directory / "mounted",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert blocker.stdout is not None
                readable, _, _ = select.select([blocker.stdout], [], [], 10)
                assert readable
                assert blocker.stdout.readline(64) == b"ready\n"

                with pytest.raises(PressureFailure, match="command_failed"):
                    controller.cleanup()
                retained = (
                    not controller.manifest.resources_closed
                    and (controller.directory / "pressure-manifest.json").exists()
                )
                mounted = os.path.ismount(controller.directory / "mounted")
            finally:
                try:
                    blocker.communicate(b"c", timeout=10)
                finally:
                    if blocker.poll() is None:
                        blocker.kill()
                    blocker.wait(timeout=10)
                    for stream in (blocker.stdin, blocker.stdout, blocker.stderr):
                        if stream is not None:
                            stream.close()
        finally:
            controller.cleanup()
            controller.finish_report()

        assert retained
        assert mounted
        assert controller.manifest.resources_closed


def test_unknown_image_identity_refuses_cleanup_before_detach(tmp_path: Path) -> None:
    from tests.plugins.storage.pressure_controller import PressureController, create
    from tests.plugins.storage.pressure_images import image_name
    from tests.plugins.storage.pressure_models import ImageStamp

    original = create(tmp_path / "unknown-image")
    saved = original.manifest
    try:
        with original:
            try:
                original.acquire("enospc")
            finally:
                saved = original.manifest
        image = Path(saved.directory) / image_name()
        info = image.stat()
        configuration = saved.model_copy(
            update={
                "image_stamp": ImageStamp(device=info.st_dev, inode=info.st_ino + 1)
            }
        )
        with PressureController(configuration) as controller:
            with pytest.raises(PressureFailure, match="image_identity"):
                controller.cleanup()

            assert image.exists()
            assert not controller.manifest.detached
            assert not controller.manifest.resources_closed
            assert os.path.ismount(image.parent / "mounted")
    finally:
        with PressureController(saved) as cleanup:
            cleanup.cleanup()
            cleanup.finish_report()


def test_pressure_owner_recovers_interrupted_manifest_write(tmp_path: Path) -> None:
    directory = tmp_path / "pending-manifest"
    process = subprocess.Popen(
        controller_arguments(directory, "pending-manifest-write"),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 30)
        assert ready
        response = process.stdout.readline(1024)
        assert response == b"manifest_pending\n", controller_diagnostics(directory)
        pending = directory / "pressure-manifest.pending"
        assert pending.exists()
        process.kill()
        process.wait(timeout=10)

        recover_owner(directory)
        report = read_report(directory)
    finally:
        close_controller(process, directory)

    assert report.resources_closed
    assert not pending.exists()
    assert not (directory / "mounted").exists()


def test_pressure_recovery_lease_refuses_a_concurrent_owner(tmp_path: Path) -> None:
    directory = tmp_path / "concurrent-recovery"
    original = subprocess.Popen(
        controller_arguments(directory, "partial-acquisition"),
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert original.stdout is not None
        ready, _, _ = select.select([original.stdout], [], [], 30)
        assert ready
        assert original.stdout.readline(1024) == b"image_created\n"
        original.kill()
        original.wait(timeout=10)
        recovery = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.plugins.storage.pressure_controller",
                "--recover",
                str(directory / "pressure-manifest.json"),
                "--workload",
                "recovery-paused",
            ],
            cwd=Path(__file__).resolve().parents[3],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert recovery.stdout is not None
            ready, _, _ = select.select([recovery.stdout], [], [], 30)
            assert ready
            assert recovery.stdout.readline(1024) == b"recovery_owned\n"

            contender = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.plugins.storage.pressure_controller",
                    "--recover",
                    str(directory / "pressure-manifest.json"),
                ],
                cwd=Path(__file__).resolve().parents[3],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert contender.returncode != 0
            assert b"controller_busy" in contender.stderr
            assert (directory / "pressure-manifest.json").exists()
        finally:
            close_controller(recovery, directory)
        report = read_report(directory)
    finally:
        close_controller(original, directory)

    assert report.resources_closed
