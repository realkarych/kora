import errno
import os
import plistlib
import re
import select
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from tests.measurements.storage_pressure_evidence import preserve_owner
from tests.plugins.storage.pressure_holders import ENTRY_NAMES, HolderObserver
from tests.plugins.storage.pressure_models import (
    PressureFailure,
    PressureReport,
    child_diagnostic,
)
from tests.plugins.storage.pressure_native import (
    collect_process,
    command,
    process_token,
)

REPOSITORY: Path = Path(__file__).resolve().parents[3]


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


def start_controller(directory: Path, workload: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        controller_arguments(directory, workload),
        cwd=REPOSITORY,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def next_signal(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], 30)
    assert ready
    return process.stdout.readline(1024)


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
            cwd=REPOSITORY,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, child_diagnostic(result.stderr)


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


def controller_diagnostics(directory: Path) -> str:
    result: list[str] = []
    for name in ("report.json", "command-stderr.txt"):
        path = directory / name
        if path.exists():
            with path.open("rb") as source:
                result.append(name + " " + child_diagnostic(source.read(16384), 8192))
        else:
            result.append(name + " absent")
    return "\n".join(result)


def run_controller(
    directory: Path, workload: str
) -> tuple[subprocess.CompletedProcess[bytes], PressureReport]:
    process = start_controller(directory, workload)
    try:
        output, error_output, status = collect_process(process, 120)
        return (
            subprocess.CompletedProcess(process.args, status, output, error_output),
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
            cwd=REPOSITORY,
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

        assert retained_process is None
        assert report.failure == "command_failed"
        assert report.resources_closed
        assert manifest.resources_closed
        assert manifest.child is not None
        assert process_token(manifest.child.pid) is None
    finally:
        try:
            if process is not None:
                close_controller(process, directory)
        finally:
            os.close(writer)
            os.close(reader)


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
    process = start_controller(directory, "parent-eof")

    try:
        signal = next_signal(process)
        assert signal == b"provider_started\n", controller_diagnostics(directory)
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
    process = start_controller(directory, workload)
    assert process.stdin is not None

    try:
        phase = next_signal(process)
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

    assert status != 0, child_diagnostic(error_output)
    assert report.provider_started, child_diagnostic(error_output)
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
        cwd=REPOSITORY,
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
        cwd=REPOSITORY,
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

    assert result.returncode == 0, (
        child_diagnostic(result.stderr) + "\n" + report.model_dump_json()
    )
    assert report.provider_started, child_diagnostic(result.stderr)
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
    assert report.provider_started, child_diagnostic(result.stderr)
    assert report.failure == "child_failure", child_diagnostic(result.stderr)
    assert report.resources_closed, child_diagnostic(result.stderr)
    assert not (directory / "mounted").exists()


def test_storage_pressure_controller_recovers_partial_acquisition(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "partial"
    process = start_controller(directory, "partial-acquisition")

    try:
        assert next_signal(process) == b"image_created\n"
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
            cwd=REPOSITORY,
            capture_output=True,
            timeout=60,
            check=False,
        )
    finally:
        close_controller(process, directory)

    report = read_report(directory)
    assert recovered.returncode == 0, child_diagnostic(recovered.stderr)
    assert report.resources_closed
    assert os.stat(directory).st_mode & 0o777 == 0o700


def test_pressure_owner_reconciles_lost_attachment_response(tmp_path: Path) -> None:
    directory = tmp_path / "lost-attachment"
    process = start_controller(directory, "lost-attachment-response")
    try:
        signal = next_signal(process)
        assert signal == b"attachment_created\n", controller_diagnostics(directory)
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
    record_testsuite_property: Callable[[str, str], None],
) -> None:
    from tests.plugins.storage.pressure_controller import create
    from tests.plugins.storage.pressure_images import discover

    with create(
        tmp_path / "busy-mount", observer=HolderObserver(budget_clock=lambda: 0)
    ) as controller:
        try:
            controller.acquire("enospc")
            stamp = controller.manifest.image_stamp
            assert stamp is not None
            attachment = discover(controller.directory, stamp)
            assert attachment is not None
            blocker = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import mmap,os,sys; held=open('holder','w+b'); "
                    "held.write(b'x'); held.flush(); "
                    "mapping=mmap.mmap(held.fileno(),1,access=mmap.ACCESS_READ); "
                    "raw=os.open(sys.argv[1],os.O_RDONLY) if sys.argv[1]!='-' else -1; "
                    "print('ready',flush=True); sys.stdin.buffer.read(1); "
                    "mapping.close(); held.close(); os.close(raw) if raw>=0 else None",
                    "/dev/r" + attachment.device[5:]
                    if sys.platform == "darwin"
                    else "-",
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
                token = process_token(blocker.pid)
                assert token is not None

                with pytest.raises(PressureFailure, match="command_failed") as failure:
                    controller.cleanup()
                retained = (
                    not controller.manifest.resources_closed
                    and (controller.directory / "pressure-manifest.json").exists()
                )
                mounted = os.path.ismount(controller.directory / "mounted")
                assert failure.value.code == "command_failed"
                if sys.platform == "darwin":
                    assert str(failure.value).startswith(
                        "command_failed: command=hdiutil detach exit="
                    )
                    notes = failure.value.__notes__
                    assert len(notes) == 1
                    note = notes[0]
                    assert note.startswith("holder_observation v1 binding=unchanged"), (
                        note
                    )
                    assert (
                        "target=volume capture=mounted "
                        "before=mounted after=mounted matching=on" in note
                    ), note
                    assert attachment.entities
                    for entity in attachment.entities:
                        for kind in ("block-", "raw-"):
                            assert (
                                f"target={kind}{entity[5:]} before=bound after=bound"
                                in note
                            ), note
                    assert "errors layout=none" in note, note
                    assert "identity_mismatch" not in note, note
                    assert "truncated=none" in note, note
                    counts = re.search(
                        r"processes=([0-9]+) visited=([0-9]+)/([0-9]+) "
                        r"same_uid_processes=([0-9]+) other_uid_processes=([0-9]+)",
                        note,
                    )
                    assert counts is not None, note
                    assert counts[1] == counts[2] == counts[3], note
                    assert all(int(value) > 0 for value in counts.groups()), note
                    candidate = next(
                        line
                        for line in note.splitlines()
                        if line.startswith("candidate prefix=diskarbitrationd ")
                    )
                    assert "uid=0 " in candidate, note
                    assert re.search(r"uniqueid=[1-9][0-9]* ", candidate), note
                    assert "after_detach=no" in candidate, note
                    entries = next(
                        line
                        for line in note.splitlines()
                        if line.startswith("volume_entries=")
                    )
                    assert all(
                        re.search(
                            re.escape(name) + r"=(dir|file|link|other|absent)", entries
                        )
                        for name in ENTRY_NAMES
                    ), note
                    holder = next(
                        line
                        for line in note.splitlines()
                        if line.startswith(f"holder pid={blocker.pid} ")
                    )
                    assert f"uniqueid={token.identity.split(':')[1]} " in holder, note
                    assert f"uid={os.geteuid()} " in holder, note
                    assert any(
                        f"comm={Path(path).name[:16]} " in holder
                        for path in (sys.executable, os.path.realpath(sys.executable))
                    ), note
                    assert all(
                        value in holder
                        for value in (
                            "identity=stable",
                            "role=fixture-descendant",
                            "executable=fixture-python",
                            "after_detach=no",
                            "cwd/volume",
                            "mapped/volume",
                        )
                    ), note
                    assert re.search(r"fd/[0-9]+/REG/volume", holder), note
                    assert re.search(
                        r"fd/[0-9]+/CHR/raw-" + re.escape(attachment.device[5:]), holder
                    ), note
                    assert "errors=" not in holder, note
                    assert all(
                        "error." not in line
                        and "denied" not in line
                        and "absent" not in line
                        for line in note.splitlines()
                        if line.startswith("target=")
                    ), note
                    assert len(note.encode()) <= 6144
                    assert "::" not in note
                    assert all(
                        character == "\n" or 32 <= ord(character) <= 126
                        for character in note
                    )
                    status = re.search(r" exit=([0-9]+) ", str(failure.value))
                    assert status is not None
                    assert controller.manifest.command_exit_status == int(status[1])
                    assert (
                        controller.directory / "command-stderr.txt"
                    ).read_bytes() == failure.value.command_stderr
                    timing = next(
                        line
                        for line in note.splitlines()
                        if line.startswith("elapsed_ms ")
                    )
                    stderr = failure.value.command_stderr or b""
                    phase = (
                        "unmount"
                        if b"couldn't unmount" in stderr
                        else "eject"
                        if b"couldn't eject" in stderr
                        else "other"
                    )
                    record_testsuite_property("pressure_holder_observation_ms", timing)
                    record_testsuite_property("pressure_busy_detach_phase", phase)
                else:
                    assert not hasattr(failure.value, "__notes__")
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
    process = start_controller(directory, "pending-manifest-write")
    try:
        response = next_signal(process)
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
    original = start_controller(directory, "partial-acquisition")
    try:
        assert next_signal(original) == b"image_created\n"
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
            cwd=REPOSITORY,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert next_signal(recovery) == b"recovery_owned\n"

            contender = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.plugins.storage.pressure_controller",
                    "--recover",
                    str(directory / "pressure-manifest.json"),
                ],
                cwd=REPOSITORY,
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
