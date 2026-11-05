import errno
import json
import os
import stat
from pathlib import Path
from typing import Literal, NoReturn, TypeAlias, TypeVar

from pydantic import BaseModel, ValidationError, field_validator

from tests.plugins.storage.pressure_models import (
    PressureManifest,
    PressureReport,
    child_diagnostic,
)

RecordState: TypeAlias = Literal[
    "ok", "absent", "unavailable", "unsafe", "oversized", "invalid", "changed"
]
Scalar: TypeAlias = bool | int | RecordState | Literal["unknown", "none"]
Model = TypeVar("Model", bound=BaseModel)
INPUT_LIMIT = 65536
STREAM_LIMIT = 1048576
OUTPUT_LIMIT = 8192
MANIFEST_FIELDS = (
    "image_requested",
    "attachment_requested",
    "mount_requested",
    "command_inflight",
    "detached",
    "manifest_resources_closed",
)
REPORT_FIELDS = ("provider_started", "cleanup_completed", "report_resources_closed")
PRESENCE_FIELDS = ("controller", "child", "command", "image_stamp")


def explicit_version(value: object) -> Literal[1]:
    if type(value) is not int or value != 1:
        raise ValueError("invalid record version")
    return 1


class RecordedManifest(PressureManifest):
    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> Literal[1, 2]:
        if type(value) is not int or value not in (1, 2):
            raise ValueError("invalid record version")
        return 1 if value == 1 else 2


class RecordedReport(PressureReport):
    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> Literal[1]:
        return explicit_version(value)


class InspectionFailure(RuntimeError):
    state: RecordState

    def __init__(self, state: RecordState) -> None:
        super().__init__(state)
        self.state = state


def require_owned(information: os.stat_result, kind: int, uid: int) -> None:
    if stat.S_IFMT(information.st_mode) != kind or information.st_uid != uid:
        raise InspectionFailure("unsafe")


def open_directory(directory: Path) -> int:
    parts = directory.absolute().parts
    if ".." in parts:
        raise InspectionFailure("unsafe")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in parts[1:]:
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        require_owned(os.fstat(descriptor), stat.S_IFDIR, os.geteuid())
        return os.dup(descriptor)
    finally:
        os.close(descriptor)


def fingerprint(information: os.stat_result) -> tuple[int, ...]:
    return (
        information.st_dev,
        information.st_ino,
        information.st_mode,
        information.st_uid,
        information.st_size,
        information.st_mtime_ns,
        information.st_ctime_ns,
    )


def require_unchanged(before: os.stat_result, after: os.stat_result) -> None:
    if fingerprint(before) != fingerprint(after):
        raise InspectionFailure("changed")


def error_state(error: OSError | InspectionFailure) -> RecordState:
    if isinstance(error, InspectionFailure):
        return error.state
    if error.errno == errno.ENOENT:
        return "absent"
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return "unsafe"
    return "unavailable"


def read_record(
    parent: int, name: str, model: type[Model]
) -> tuple[RecordState, Model | None]:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        require_owned(before, stat.S_IFREG, os.geteuid())
        if before.st_size > INPUT_LIMIT:
            raise InspectionFailure("oversized")
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            require_unchanged(before, os.fstat(descriptor))
            data = bytearray()
            while len(data) < before.st_size:
                chunk = os.read(descriptor, before.st_size - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            require_unchanged(before, os.fstat(descriptor))
            require_unchanged(
                before, os.stat(name, dir_fd=parent, follow_symlinks=False)
            )
            if len(data) != before.st_size:
                raise InspectionFailure("changed")
        finally:
            os.close(descriptor)
    except (OSError, InspectionFailure) as error:
        return error_state(error), None
    try:
        return "ok", model.model_validate_json(data)
    except ValidationError:
        return "invalid", None


def stream_size(parent: int, name: str) -> tuple[RecordState, Scalar]:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        require_owned(before, stat.S_IFREG, os.geteuid())
        if before.st_size > STREAM_LIMIT:
            raise InspectionFailure("oversized")
        require_unchanged(before, os.stat(name, dir_fd=parent, follow_symlinks=False))
        return "ok", before.st_size
    except (OSError, InspectionFailure) as error:
        return error_state(error), "unknown"


def initial_summary(state: RecordState) -> dict[str, Scalar]:
    return {
        "summary_version": 1,
        **dict.fromkeys(
            (f"{name}_state" for name in ("manifest", "report", "stdout", "stderr")),
            state,
        ),
        "manifest_version": "unknown",
        **dict.fromkeys(
            (f"recorded_{name}" for name in (*MANIFEST_FIELDS, *REPORT_FIELDS)),
            "unknown",
        ),
        **dict.fromkeys(
            (f"{name}_record_present" for name in PRESENCE_FIELDS), "unknown"
        ),
        "recorded_command_exit_status": "unknown",
        "report_failure_present": "unknown",
        "stdout_bytes": "unknown",
        "stderr_bytes": "unknown",
    }


def project_manifest(value: PressureManifest) -> dict[str, Scalar]:
    fields: tuple[tuple[str, str, Scalar], ...] = (
        ("manifest_version", "version", value.version),
        ("recorded_image_requested", "image_requested", value.image_requested),
        (
            "recorded_attachment_requested",
            "attachment_requested",
            value.attachment_requested,
        ),
        ("recorded_mount_requested", "mount_requested", value.mount_requested),
        ("recorded_command_inflight", "command_inflight", value.command_inflight),
        ("recorded_detached", "detached", value.detached),
        (
            "recorded_manifest_resources_closed",
            "resources_closed",
            value.resources_closed,
        ),
        ("controller_record_present", "controller", True),
        ("child_record_present", "child", value.child is not None),
        ("command_record_present", "command", value.command is not None),
        ("image_stamp_record_present", "image_stamp", value.image_stamp is not None),
        (
            "recorded_command_exit_status",
            "command_exit_status",
            value.command_exit_status
            if value.command_exit_status is not None
            else "none",
        ),
    )
    return {
        key: observed if source in value.model_fields_set else "unknown"
        for key, source, observed in fields
    }


def project_report(value: PressureReport) -> dict[str, Scalar]:
    fields: tuple[tuple[str, str, Scalar], ...] = (
        ("recorded_provider_started", "provider_started", value.provider_started),
        ("recorded_cleanup_completed", "cleanup_completed", value.cleanup_completed),
        (
            "recorded_report_resources_closed",
            "resources_closed",
            value.resources_closed,
        ),
        ("report_failure_present", "failure", value.failure is not None),
    )
    return {
        key: observed if source in value.model_fields_set else "unknown"
        for key, source, observed in fields
    }


def inspect_directory(directory: Path) -> dict[str, Scalar]:
    descriptor = open_directory(directory)
    try:
        before = os.fstat(descriptor)
        result = initial_summary("unavailable")
        result["manifest_state"], manifest = read_record(
            descriptor, "pressure-manifest.json", RecordedManifest
        )
        result["report_state"], report = read_record(
            descriptor, "report.json", RecordedReport
        )
        if manifest is not None:
            result.update(project_manifest(manifest))
        if report is not None:
            result.update(project_report(report))
        for stream in ("stdout", "stderr"):
            result[f"{stream}_state"], result[f"{stream}_bytes"] = stream_size(
                descriptor, f"command-{stream}.txt"
            )
        current = open_directory(directory)
        try:
            after = os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise InspectionFailure("changed")
        finally:
            os.close(current)
        return result
    finally:
        os.close(descriptor)


def recovery_summary(directory: Path) -> str:
    try:
        values = inspect_directory(directory)
    except (OSError, InspectionFailure) as error:
        values = initial_summary(error_state(error))
    rendered = json.dumps(values, sort_keys=True, separators=(",", ":"))
    if len(rendered.encode()) > OUTPUT_LIMIT:
        raise ValueError("recovery summary exceeds its output bound")
    return rendered


def raise_recovery_failure(directory: Path, stderr: bytes) -> NoReturn:
    try:
        raise AssertionError(child_diagnostic(stderr))
    except AssertionError as failure:
        failure.add_note(recovery_summary(directory))
        raise
