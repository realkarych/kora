import json
import os
import stat
from pathlib import Path

from pydantic import ValidationError

from tests.plugins.storage.pressure_models import (
    NativeOutcome,
    NativeRequest,
    PressureFailure,
)

NATIVE_FILES: tuple[str, ...] = (
    "native-result.json",
    "native-result.pending",
    "native-text.bin",
    "native-text.pending",
    "native-state.json",
    "native-command-stderr.txt",
    "native-command-stdout.txt",
    "native-command-stderr.pending",
    "native-command-stdout.pending",
    "unmount-status.bin",
    "eject-status.bin",
)


def read_outcome(directory: Path, request: NativeRequest) -> NativeOutcome:
    path = directory / "native-result.json"
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > 8192
        ):
            raise PressureFailure("native_outcome_identity")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            encoded = stream.read(8193)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        snapshots = (before, opened, after, current)
        fingerprints = {
            (
                value.st_dev,
                value.st_ino,
                value.st_uid,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            for value in snapshots
        }
        if len(fingerprints) != 1 or len(encoded) != before.st_size:
            raise PressureFailure("native_outcome_changed")
        outcome = NativeOutcome.model_validate_json(encoded)
    except (OSError, ValidationError):
        raise PressureFailure("native_completion_unknown") from None
    if (
        request.helper is None
        or outcome.helper != request.helper
        or outcome.request_id != request.request_id
        or outcome.phase != request.phase
    ):
        raise PressureFailure("native_outcome_mismatch")
    return outcome


def reported_value(known: bool, value: int | str | None) -> int | str:
    if not known:
        return "unknown"
    return "none" if value is None else value


def native_note(request: NativeRequest) -> str:
    outcome = request.outcome
    known = outcome is not None
    return json.dumps(
        {
            "native_summary_version": 2,
            "phase": request.phase,
            "result": "unknown" if outcome is None else outcome.result,
            "recorded_completion_known": outcome is not None,
            "recorded_cf_closed": "unknown" if outcome is None else outcome.cf_closed,
            "code": "unknown" if outcome is None else outcome.code,
            "dissenter_pid": reported_value(
                known, None if outcome is None else outcome.dissenter_pid
            ),
            "dissenter_comm": reported_value(
                known, None if outcome is None else outcome.dissenter_comm
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
