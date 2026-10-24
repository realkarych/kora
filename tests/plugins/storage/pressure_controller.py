import argparse
import fcntl
import multiprocessing
import os
import select
import stat
import subprocess
import sys
import time
import uuid
from multiprocessing.connection import Connection
from pathlib import Path
from types import TracebackType
from typing import Self

from kora.plugins.storage.files import DIRECTORY_FLAGS, FileLease, trusted_directory
from tests.plugins.storage import pressure_images
from tests.plugins.storage.pressure_models import (
    ImageStamp,
    PressureFailure,
    PressureManifest,
    PressureReport,
    ProcessToken,
)
from tests.plugins.storage.pressure_native import (
    collect_process,
    command,
    process_token,
    wait_closed,
)
from tests.plugins.storage.pressure_workload import PressureStart


def synchronize_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def open_directory(path: Path, *, create: bool = False) -> tuple[Path, int]:
    absolute = path.absolute()
    if (
        len(os.fsencode(absolute)) > 4096
        or len(absolute.parts) > 128
        or ".." in absolute.parts
    ):
        raise PressureFailure("directory_path")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        previous = trusted_directory(descriptor, None)
        for index, component in enumerate(absolute.parts[1:]):
            if create and index == len(absolute.parts) - 2:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            previous = trusted_directory(descriptor, previous)
        if previous.st_uid != os.geteuid() or stat.S_IMODE(previous.st_mode) != 0o700:
            raise PressureFailure("directory_identity")
        return absolute, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def save(path: Path, encoded: bytes, *, hold_pending: bool = False) -> None:
    pending = path.with_suffix(".pending")
    try:
        previous = os.open(pending, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        previous = None
    if previous is not None:
        try:
            info = os.fstat(previous)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > 1048576
            ):
                raise PressureFailure("pending_identity")
        finally:
            os.close(previous)
        pending.unlink()
        synchronize_directory(path.parent)
    descriptor = os.open(
        pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if hold_pending:
        sys.stdout.write("manifest_pending\n")
        sys.stdout.flush()
        if sys.stdin.buffer.read(1) != b"c":
            raise PressureFailure("controller_control_closed")
    os.replace(pending, path)
    synchronize_directory(path.parent)


def acquire_lease(directory: Path, stamp: ImageStamp | None = None) -> FileLease:
    _, anchor = open_directory(directory)
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if stamp is None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open("controller.lock", flags, 0o600, dir_fd=anchor)
        try:
            information = os.fstat(descriptor)
            if (
                not stat.S_ISREG(information.st_mode)
                or information.st_uid != os.geteuid()
                or stat.S_IMODE(information.st_mode) != 0o600
                or information.st_nlink != 1
                or (
                    stamp is not None
                    and stamp
                    != ImageStamp(device=information.st_dev, inode=information.st_ino)
                )
            ):
                raise PressureFailure("lease_identity")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PressureFailure("controller_busy") from None
            os.fsync(descriptor)
            os.fsync(anchor)
            return FileLease(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(anchor)


def read_manifest(path: Path) -> PressureManifest:
    information = path.lstat()
    if (
        not stat.S_ISREG(information.st_mode)
        or information.st_uid != os.geteuid()
        or stat.S_IMODE(information.st_mode) != 0o600
        or information.st_nlink != 1
        or information.st_size > 16384
    ):
        raise PressureFailure("manifest_identity")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        encoded = stream.read(16385)
    manifest = PressureManifest.model_validate_json(encoded)
    directory = path.parent
    actual = directory.lstat()
    if (
        directory.resolve() != directory
        or not stat.S_ISDIR(actual.st_mode)
        or actual.st_uid != os.geteuid()
        or stat.S_IMODE(actual.st_mode) != 0o700
        or manifest.directory != str(directory)
        or manifest.uid != os.geteuid()
        or manifest.platform != sys.platform
        or manifest.directory_stamp
        != ImageStamp(device=actual.st_dev, inode=actual.st_ino)
    ):
        raise PressureFailure("manifest_identity")
    return manifest


class PressureController:
    def __init__(
        self, manifest: PressureManifest, lease: FileLease | None = None
    ) -> None:
        self.manifest = manifest
        self.directory = Path(manifest.directory)
        self.lease = (
            lease
            if lease is not None
            else acquire_lease(self.directory, manifest.lease_stamp)
        )
        self.child: subprocess.Popen[bytes] | None = None
        self.connection: Connection | None = None
        self.report = PressureReport()
        self.started = time.monotonic()
        self.parent_descriptor: int | None = None
        self.cleaning = False
        self.pause_response = False
        self.hold_pending = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.lease.close()

    def validate_directory(self) -> None:
        if self.lease.closed:
            raise PressureFailure("controller_closed")
        actual = self.directory.lstat()
        canonical, descriptor = open_directory(self.directory)
        try:
            anchored = os.fstat(descriptor)
            if (
                canonical != self.directory
                or ImageStamp(device=actual.st_dev, inode=actual.st_ino)
                != self.manifest.directory_stamp
                or (anchored.st_dev, anchored.st_ino) != (actual.st_dev, actual.st_ino)
            ):
                raise PressureFailure("directory_identity")
        finally:
            os.close(descriptor)

    def check_parent(self) -> None:
        if self.parent_descriptor is None or self.cleaning:
            return
        ready, _, _ = select.select([self.parent_descriptor], [], [], 0)
        if ready:
            signal = os.read(self.parent_descriptor, 1)
            raise PressureFailure(
                "controller_cancelled"
                if signal == b"c"
                else "controller_control_closed"
            )

    def receive(self, deadline: float) -> bytes:
        if self.connection is None:
            raise PressureFailure("child_state_invalid")
        descriptors = [self.connection.fileno()]
        if self.parent_descriptor is not None:
            descriptors.append(self.parent_descriptor)
        ready, _, _ = select.select(
            descriptors, [], [], max(0, deadline - time.monotonic())
        )
        self.check_parent()
        if self.connection.fileno() not in ready:
            raise PressureFailure("child_deadline")
        return self.connection.recv_bytes(16384)

    def persist(self) -> None:
        self.validate_directory()
        save(
            self.directory / "pressure-manifest.json",
            self.manifest.model_dump_json().encode(),
            hold_pending=self.hold_pending,
        )

    def effect(self, arguments: tuple[str, ...]) -> bytes:
        self.validate_directory()
        self.check_parent()
        self.manifest = self.manifest.model_copy(
            update={
                "command_inflight": True,
                "command": None,
                "command_exit_status": None,
            }
        )
        self.persist()
        return command(
            arguments,
            on_closed=self.command_closed,
            on_started=self.command_started,
            on_finished=self.command_finished,
        )

    def command_started(self, token: ProcessToken) -> None:
        self.manifest = self.manifest.model_copy(update={"command": token})
        self.persist()

    def command_finished(self, status: int) -> None:
        self.manifest = self.manifest.model_copy(update={"command_exit_status": status})
        self.persist()

    def command_closed(self) -> None:
        if self.pause_response:
            sys.stdout.write("attachment_created\n")
            sys.stdout.flush()
            if sys.stdin.buffer.read(1) != b"c":
                raise PressureFailure("controller_control_closed")
        self.manifest = self.manifest.model_copy(update={"command_inflight": False})
        self.persist()

    def start_child(self) -> None:
        parent, child = multiprocessing.get_context("spawn").Pipe(duplex=True)
        try:
            self.child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tests.plugins.storage.pressure_workload",
                    str(child.fileno()),
                ],
                cwd=Path(__file__).resolve().parents[3],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(child.fileno(),),
            )
        finally:
            child.close()
        self.connection = parent
        if not parent.poll(15) or parent.recv_bytes(64) != b"ready":
            raise PressureFailure("child_start_failed")
        token = process_token(self.child.pid)
        if token is None:
            raise PressureFailure("child_start_failed")
        self.manifest = self.manifest.model_copy(update={"child": token})
        self.persist()

    def acquire(self, workload: str) -> None:
        self.manifest = self.manifest.model_copy(update={"image_requested": True})
        self.persist()
        image = self.directory / pressure_images.image_name()
        if workload == "partial-acquisition":
            descriptor = os.open(
                image, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.close(descriptor)
            synchronize_directory(self.directory)
            sys.stdout.write("image_created\n")
            sys.stdout.flush()
            if sys.stdin.buffer.read(1) != b"c":
                raise PressureFailure("controller_control_closed")
            raise PressureFailure("partial_acquisition_released")
        pressure_images.create_image(self.directory, self.manifest.nonce, self.effect)
        stamp = pressure_images.inspect_image(image, None)
        self.manifest = self.manifest.model_copy(update={"image_stamp": stamp})
        self.persist()
        (self.directory / "mounted").mkdir(mode=0o700)
        synchronize_directory(self.directory)
        self.manifest = self.manifest.model_copy(
            update={
                "attachment_requested": True,
                "mount_requested": sys.platform == "darwin",
            }
        )
        self.persist()
        self.pause_response = workload == "lost-attachment-response"
        try:
            pressure_images.attach(self.directory, self.effect)
        finally:
            self.pause_response = False
        if workload == "pending-manifest-write":
            self.hold_pending = True
            try:
                self.persist()
            finally:
                self.hold_pending = False
        attachment = pressure_images.discover(self.directory, stamp)
        if attachment is None:
            raise PressureFailure("attachment_missing")
        self.manifest = self.manifest.model_copy(update={"mount_requested": True})
        self.persist()
        pressure_images.mount(self.directory, attachment, self.effect)
        mounted = pressure_images.discover(self.directory, stamp)
        if mounted is None:
            raise PressureFailure("mount_missing")
        filesystem = pressure_images.verify_mount(self.directory, mounted)
        self.report = self.report.model_copy(update={"filesystem": filesystem})

    def run_child(self, workload: str) -> None:
        held = workload in {"parent-eof", "cancelled", "deadline"}
        if (
            self.child is None
            or self.connection is None
            or (not held and workload not in {"enospc", "child-failure"})
        ):
            raise PressureFailure("child_state_invalid")
        start = PressureStart.model_validate(
            {
                "mount": str(self.directory / "mounted"),
                "workload": "hold" if held else workload,
            }
        )
        self.check_parent()
        self.connection.send_bytes(start.model_dump_json().encode())
        deadline = time.monotonic() + 90
        try:
            if self.receive(deadline) != b"provider_started":
                raise PressureFailure("provider_start_failed")
            self.report = self.report.model_copy(update={"provider_started": True})
            if held:
                sys.stdout.write("provider_started\n")
                sys.stdout.flush()
            if workload == "deadline":
                deadline = time.monotonic() + 0.01
            result = PressureReport.model_validate_json(self.receive(deadline))
        except EOFError:
            result = None
        if result is not None:
            save(
                self.directory / "child-report.json", result.model_dump_json().encode()
            )
            self.report = result.model_copy(
                update={
                    "filesystem": self.report.filesystem,
                    "provider_started": self.report.provider_started
                    or result.provider_started,
                }
            )
        self.connection.close()
        self.connection = None
        output, error_output, status = collect_process(self.child, 10)
        save(self.directory / "child-stderr.txt", error_output)
        if output:
            raise PressureFailure("child_stdout_not_empty")
        if status != 0 or result is None:
            raise PressureFailure(
                result.failure
                if result is not None and result.failure is not None
                else "child_failure"
            )

    def close_child(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.child is not None:
            try:
                if self.child.poll() is None:
                    self.child.kill()
                self.child.wait(timeout=10)
            finally:
                if self.child.stderr is not None:
                    self.child.stderr.close()
                if self.child.stdout is not None:
                    self.child.stdout.close()
            return
        if self.manifest.child is not None:
            wait_closed(self.manifest.child, 15)

    def cleanup(self) -> None:
        self.cleaning = True
        self.validate_directory()
        self.close_child()
        if self.manifest.command_inflight:
            if (
                self.manifest.command is None
                or self.manifest.command_exit_status is None
            ):
                raise PressureFailure("acquisition_unresolved")
            wait_closed(self.manifest.command, 30)
            self.manifest = self.manifest.model_copy(update={"command_inflight": False})
            self.persist()
        image = self.directory / pressure_images.image_name()
        if not self.manifest.detached:
            if image.exists() or image.is_symlink():
                if not self.manifest.image_requested:
                    raise PressureFailure("image_without_intent")
                stamp = pressure_images.inspect_image(image, self.manifest.image_stamp)
                self.manifest = self.manifest.model_copy(update={"image_stamp": stamp})
                self.persist()
                if self.manifest.attachment_requested:
                    pressure_images.detach(self.directory, stamp, self.effect)
            elif self.manifest.attachment_requested:
                raise PressureFailure("attachment_unresolved")
            self.manifest = self.manifest.model_copy(update={"detached": True})
            self.persist()
        if image.exists() or image.is_symlink():
            pressure_images.inspect_image(image, self.manifest.image_stamp)
            image.unlink()
            synchronize_directory(self.directory)
        mountpoint = self.directory / "mounted"
        if mountpoint.exists() or mountpoint.is_symlink():
            information = mountpoint.lstat()
            if (
                not stat.S_ISDIR(information.st_mode)
                or information.st_uid != os.geteuid()
                or stat.S_IMODE(information.st_mode) != 0o700
                or os.path.ismount(mountpoint)
            ):
                raise PressureFailure("mountpoint_identity")
            mountpoint.rmdir()
            synchronize_directory(self.directory)
        source = self.directory / "volume-root"
        if source.exists() or source.is_symlink():
            information = source.lstat()
            if (
                not stat.S_ISDIR(information.st_mode)
                or information.st_uid != os.geteuid()
                or stat.S_IMODE(information.st_mode) != 0o700
                or information.st_dev != self.manifest.directory_stamp.device
            ):
                raise PressureFailure("source_directory_identity")
            source.rmdir()
            synchronize_directory(self.directory)
        self.manifest = self.manifest.model_copy(update={"resources_closed": True})
        self.persist()
        self.report = self.report.model_copy(update={"resources_closed": True})

    def finish_report(self) -> None:
        self.validate_directory()
        self.report = self.report.model_copy(
            update={"elapsed_seconds": time.monotonic() - self.started}
        )
        save(self.directory / "report.json", self.report.model_dump_json().encode())


def create(directory: Path) -> PressureController:
    if sys.platform not in {"darwin", "linux"}:
        raise PressureFailure("platform_unsupported")
    canonical, descriptor = open_directory(directory, create=True)
    try:
        information = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    lease = acquire_lease(canonical)
    try:
        lease_info = os.fstat(lease.fd)
        token = process_token(os.getpid())
        if token is None:
            raise PressureFailure("controller_identity")
        manifest = PressureManifest.model_validate(
            {
                "version": 1,
                "platform": sys.platform,
                "uid": os.geteuid(),
                "directory": str(canonical),
                "directory_stamp": {
                    "device": information.st_dev,
                    "inode": information.st_ino,
                },
                "nonce": uuid.uuid4().hex,
                "controller": token.model_dump(),
                "lease_stamp": {
                    "device": lease_info.st_dev,
                    "inode": lease_info.st_ino,
                },
            }
        )
        controller = PressureController(manifest, lease)
        controller.persist()
        return controller
    except BaseException:
        lease.close()
        raise


def run(directory: Path, workload: str) -> int:
    with create(directory) as controller:
        controller.parent_descriptor = sys.stdin.fileno()
        succeeded = False
        try:
            try:
                controller.start_child()
                controller.acquire(workload)
                controller.run_child(workload)
                succeeded = controller.report.failure is None
            except PressureFailure as error:
                controller.report = controller.report.model_copy(
                    update={"failure": error.code}
                )
        finally:
            try:
                controller.cleanup()
            finally:
                controller.finish_report()
        return 0 if succeeded else 1


def recover(path: Path, *, hold: bool = False) -> int:
    manifest = read_manifest(path.absolute())
    with PressureController(manifest) as controller:
        current = read_manifest(path.absolute())
        if current.lease_stamp != manifest.lease_stamp:
            raise PressureFailure("lease_identity")
        if process_token(current.controller.pid) == current.controller:
            raise PressureFailure("controller_still_active")
        controller.validate_directory()
        token = process_token(os.getpid())
        if token is None:
            raise PressureFailure("controller_identity")
        controller.manifest = current.model_copy(update={"controller": token})
        controller.persist()
        if hold:
            sys.stdout.write("recovery_owned\n")
            sys.stdout.flush()
            if sys.stdin.buffer.read(1) != b"c":
                raise PressureFailure("controller_control_closed")
        report_path = path.parent / "report.json"
        if report_path.exists():
            controller.report = PressureReport.model_validate_json(
                report_path.read_bytes()
            )
        try:
            controller.cleanup()
        finally:
            controller.finish_report()
        return 0


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--recover", type=Path)
    parser.add_argument(
        "--workload",
        choices=(
            "enospc",
            "child-failure",
            "partial-acquisition",
            "lost-attachment-response",
            "pending-manifest-write",
            "recovery-paused",
            "parent-eof",
            "cancelled",
            "deadline",
        ),
        default="enospc",
    )
    arguments = parser.parse_args()
    directory: Path | None = arguments.directory
    recovery: Path | None = arguments.recover
    workload: str = arguments.workload
    if recovery is not None and directory is None:
        return recover(recovery, hold=workload == "recovery-paused")
    if directory is not None and recovery is None:
        return run(directory, workload)
    raise PressureFailure("controller_arguments")


if __name__ == "__main__":
    raise SystemExit(main())
