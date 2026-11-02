import json
import os
import stat
import traceback
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from tests.plugins.storage.pressure_models import PressureReport
from tests.plugins.storage.recovery_summary import (
    INPUT_LIMIT,
    OUTPUT_LIMIT,
    STREAM_LIMIT,
    InspectionFailure,
    open_directory,
    raise_recovery_failure,
    read_record,
    recovery_summary,
    require_owned,
    require_unchanged,
)

MARKER = "private-record-payload"
SUMMARY: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
KEYS: frozenset[str] = frozenset(
    {
        "summary_version",
        "manifest_state",
        "report_state",
        "stdout_state",
        "stderr_state",
        "manifest_version",
        "recorded_image_requested",
        "recorded_attachment_requested",
        "recorded_mount_requested",
        "recorded_command_inflight",
        "recorded_detached",
        "recorded_manifest_resources_closed",
        "controller_record_present",
        "child_record_present",
        "command_record_present",
        "image_stamp_record_present",
        "recorded_command_exit_status",
        "recorded_provider_started",
        "recorded_cleanup_completed",
        "recorded_report_resources_closed",
        "report_failure_present",
        "stdout_bytes",
        "stderr_bytes",
    }
)


def manifest() -> dict[str, JsonValue]:
    return {
        "version": 1,
        "platform": "darwin",
        "uid": os.geteuid(),
        "directory": MARKER,
        "directory_stamp": {"device": 1, "inode": 1},
        "nonce": "a" * 32,
        "controller": {"pid": 12345, "identity": MARKER},
        "lease_stamp": {"device": 1, "inode": 2},
    }


def decoded(directory: Path) -> dict[str, JsonValue]:
    return SUMMARY.validate_json(recovery_summary(directory))


def failure_with_cleanup(directory: Path, cleanup_fails: bool) -> None:
    try:
        raise_recovery_failure(directory, b"first refusal")
    finally:
        (directory / "report.json").write_text(
            json.dumps({"resources_closed": True, "failure": None})
        )
        if cleanup_fails:
            raise_recovery_failure(directory, b"later refusal")


def test_summary_contains_only_approved_scalars(tmp_path: Path) -> None:
    record = manifest() | {
        "command_exit_status": 17,
        "command_inflight": False,
        "detached": False,
        "resources_closed": False,
        "child": None,
    }
    (tmp_path / "pressure-manifest.json").write_text(json.dumps(record))
    (tmp_path / "report.json").write_text(
        json.dumps({"failure": MARKER, "resources_closed": False})
    )
    for stream in ("stdout", "stderr"):
        path = tmp_path / f"command-{stream}.txt"
        path.write_text(MARKER)
        path.chmod(0)
    before = frozenset(os.listdir("/dev/fd"))

    with pytest.raises(AssertionError) as failure:
        raise_recovery_failure(tmp_path, b"native refusal")
    rendered = failure.value.__notes__[0]
    exception_context = "".join(traceback.format_exception(failure.value))
    result = SUMMARY.validate_json(rendered)

    assert result.keys() == KEYS
    assert all(
        result[f"{name}_state"] == "ok"
        for name in ("manifest", "report", "stdout", "stderr")
    )
    assert result["recorded_command_exit_status"] == 17
    assert result["recorded_command_inflight"] is False
    assert result["recorded_manifest_resources_closed"] is False
    assert result["child_record_present"] is False
    assert result["controller_record_present"] is True
    assert result["report_failure_present"] is True
    assert result["stdout_bytes"] == len(MARKER)
    assert result["stderr_bytes"] == len(MARKER)
    assert MARKER not in rendered
    assert MARKER not in exception_context
    assert "12345" not in rendered
    assert str(tmp_path) not in rendered
    assert len(rendered.encode()) <= OUTPUT_LIMIT
    assert frozenset(os.listdir("/dev/fd")) == before


@pytest.mark.parametrize("state", ["omitted", "false", "null", "invalid"])
def test_persisted_fields_remain_distinct_from_defaults(
    tmp_path: Path, state: str
) -> None:
    record = manifest()
    report: dict[str, JsonValue] = {}
    if state == "false":
        record.update(
            {"resources_closed": False, "command_exit_status": 0, "child": None}
        )
        report.update({"resources_closed": False, "failure": None})
    elif state == "null":
        record["command_exit_status"] = None
        report["failure"] = None
    elif state == "invalid":
        record["resources_closed"] = None
        report["resources_closed"] = MARKER
    (tmp_path / "pressure-manifest.json").write_text(json.dumps(record))
    (tmp_path / "report.json").write_text(json.dumps(report))

    result = decoded(tmp_path)

    assert result["manifest_state"] == ("invalid" if state == "invalid" else "ok")
    assert result["report_state"] == ("invalid" if state == "invalid" else "ok")
    assert result["recorded_manifest_resources_closed"] == (
        False if state == "false" else "unknown"
    )
    assert result["recorded_report_resources_closed"] == (
        False if state == "false" else "unknown"
    )
    assert result["recorded_command_exit_status"] == {"false": 0, "null": "none"}.get(
        state, "unknown"
    )
    assert result["report_failure_present"] == (
        False if state in ("false", "null") else "unknown"
    )


@pytest.mark.parametrize("version", [True, 1.0, "1", 0])
def test_record_version_is_not_coerced(tmp_path: Path, version: JsonValue) -> None:
    record = manifest() | {"version": version, "resources_closed": True}
    report: dict[str, JsonValue] = {"version": version, "resources_closed": True}
    (tmp_path / "pressure-manifest.json").write_text(json.dumps(record))
    (tmp_path / "report.json").write_text(json.dumps(report))

    result = decoded(tmp_path)

    assert result["manifest_state"] == "invalid"
    assert result["report_state"] == "invalid"
    assert result["manifest_version"] == "unknown"
    assert result["recorded_manifest_resources_closed"] == "unknown"
    assert result["recorded_report_resources_closed"] == "unknown"


@pytest.mark.parametrize(
    "name",
    [
        "pressure-manifest.json",
        "report.json",
        "command-stdout.txt",
        "command-stderr.txt",
    ],
)
@pytest.mark.parametrize(
    "kind", ["absent", "directory", "symlink", "fifo", "oversized"]
)
def test_file_refusal_is_bounded_and_closes_descriptors(
    tmp_path: Path, name: str, kind: str
) -> None:
    path = tmp_path / name
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(tmp_path / MARKER)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversized":
        with path.open("wb") as output:
            output.truncate(
                (INPUT_LIMIT if name.endswith("json") else STREAM_LIMIT) + 1
            )
    key = {
        "pressure-manifest.json": "manifest",
        "report.json": "report",
        "command-stdout.txt": "stdout",
        "command-stderr.txt": "stderr",
    }[name]
    before = frozenset(os.listdir("/dev/fd"))

    result = decoded(tmp_path)

    assert result[f"{key}_state"] == (
        kind if kind in ("absent", "oversized") else "unsafe"
    )
    assert result.keys() == KEYS
    assert MARKER not in json.dumps(result)
    assert frozenset(os.listdir("/dev/fd")) == before


@pytest.mark.parametrize(
    "payload", [b"", b"{", b"\xff", json.dumps({"unexpected": MARKER}).encode()]
)
def test_invalid_record_content_is_not_rendered(tmp_path: Path, payload: bytes) -> None:
    (tmp_path / "pressure-manifest.json").write_bytes(payload)
    (tmp_path / "report.json").write_bytes(payload)
    before = frozenset(os.listdir("/dev/fd"))

    with pytest.raises(AssertionError) as failure:
        raise_recovery_failure(tmp_path, b"original refusal")
    rendered = "".join(traceback.format_exception(failure.value))
    result = SUMMARY.validate_json(failure.value.__notes__[0])

    assert result["manifest_state"] == "invalid"
    assert result["report_state"] == "invalid"
    assert "original refusal" in rendered
    assert MARKER not in rendered
    assert frozenset(os.listdir("/dev/fd")) == before


def test_directory_symlinks_cannot_redirect_inspection(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "report.json").write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(owned, target_is_directory=True)
    child = owned / "child"
    child.mkdir()

    direct = decoded(alias)
    ancestor = decoded(alias / "child")

    assert direct["report_state"] == "unsafe"
    assert ancestor["report_state"] == "unsafe"


def test_file_checks_refuse_changed_identity_and_wrong_owner(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("{}")
    original = path.stat()
    replacement = tmp_path / "replacement"
    replacement.write_text("{}")
    replacement.replace(path)

    with pytest.raises(InspectionFailure) as changed:
        require_unchanged(original, path.stat())
    with pytest.raises(InspectionFailure) as owner:
        require_owned(path.stat(), stat.S_IFREG, os.geteuid() + 1)

    assert changed.value.state == "changed"
    assert owner.value.state == "unsafe"


def test_record_read_honors_exact_byte_limit(tmp_path: Path) -> None:
    (tmp_path / "report.json").write_bytes(b"{}" + b" " * (INPUT_LIMIT - 2))
    before = frozenset(os.listdir("/dev/fd"))
    descriptor = open_directory(tmp_path)

    try:
        state, record = read_record(descriptor, "report.json", PressureReport)
    finally:
        os.close(descriptor)

    assert state == "ok"
    assert record is not None
    assert record.model_fields_set == set()
    assert frozenset(os.listdir("/dev/fd")) == before


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_first_failure_summary_survives_later_cleanup(
    tmp_path: Path, cleanup_fails: bool
) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"resources_closed": False, "failure": MARKER}))
    before = frozenset(os.listdir("/dev/fd"))

    with pytest.raises(AssertionError) as captured:
        failure_with_cleanup(tmp_path, cleanup_fails)
    first = captured.value.__context__ if cleanup_fails else captured.value
    assert isinstance(first, AssertionError)
    original = SUMMARY.validate_json(first.__notes__[0])
    rendered = "".join(traceback.format_exception(captured.value))

    assert original["recorded_report_resources_closed"] is False
    assert original["report_failure_present"] is True
    assert decoded(tmp_path)["recorded_report_resources_closed"] is True
    assert "first refusal" in rendered
    assert ("later refusal" in rendered) is cleanup_fails
    assert MARKER not in rendered
    assert frozenset(os.listdir("/dev/fd")) == before
