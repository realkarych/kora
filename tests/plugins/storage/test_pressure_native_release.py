import ctypes
import json
import select
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from tests.measurements.storage_pressure_evidence import preserve_owner
from tests.plugins.storage.pressure_controller import (
    PressureController,
    create,
    read_manifest,
    save,
)
from tests.plugins.storage.pressure_disk_arbitration import NativeCall, private_text
from tests.plugins.storage.pressure_images import image_name
from tests.plugins.storage.pressure_models import (
    ImageStamp,
    NativeOutcome,
    NativeRequest,
    PressureFailure,
    PressureManifest,
    ProcessToken,
)
from tests.plugins.storage.pressure_native import command, process_token
from tests.plugins.storage.pressure_native_records import native_note


@pytest.fixture
def closed_helper() -> ProcessToken:
    tokens: list[ProcessToken] = []
    command((sys.executable, "-I", "-c", "pass"), on_started=tokens.append)
    assert len(tokens) == 1
    assert process_token(tokens[0].pid) != tokens[0]
    return tokens[0]


def request_for(controller: PressureController) -> NativeRequest:
    return NativeRequest(
        version=1,
        request_id="1" * 32,
        nonce=controller.manifest.nonce,
        controller=controller.manifest.controller,
        image_stamp=ImageStamp(device=1, inode=1),
        phase="unmount",
        device="/dev/disk99999999",
        volume="/dev/disk99999999s1",
        entities=("/dev/disk99999999", "/dev/disk99999999s1"),
        deadline=time.monotonic() + 20,
        helper=None,
        outcome=None,
    )


def bind_request(controller: PressureController, request: NativeRequest) -> None:
    controller.manifest = controller.manifest.model_copy(
        update={
            "version": 2,
            "native": request,
            "image_stamp": request.image_stamp,
            "attachment_requested": True,
        }
    )
    controller.persist()


def no_request(request: NativeRequest) -> NativeOutcome:
    assert request.helper is not None
    return NativeOutcome(
        version=1,
        request_id=request.request_id,
        phase=request.phase,
        helper=request.helper,
        result="not_requested",
        code=None,
        cf_closed=True,
        text_present=False,
        text_truncated=False,
    )


@pytest.mark.parametrize("field", ["native", "helper", "outcome"])
def test_native_records_require_explicit_pending_fields(
    tmp_path: Path, field: str
) -> None:
    with create(tmp_path / "explicit") as controller:
        request = request_for(controller)
        bind_request(controller, request)
        value = controller.manifest.model_dump(mode="json")
        if field == "native":
            del value[field]
        else:
            nested = request.model_dump(mode="json")
            del nested[field]
            value["native"] = nested

        with pytest.raises(ValidationError):
            PressureManifest.model_validate(value)

        assert not controller.manifest.resources_closed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("cf_closed", 1),
        ("code", -1),
        ("code", 4294967296),
        ("code", True),
        ("result", "unknown"),
        ("text_present", True),
    ],
)
def test_native_outcome_rejects_ambiguous_completion(
    tmp_path: Path, closed_helper: ProcessToken, field: str, value: object
) -> None:
    with create(tmp_path / "ambiguous") as controller:
        request = request_for(controller).model_copy(update={"helper": closed_helper})
        fields = no_request(request).model_dump(mode="json")
        fields[field] = value

        with pytest.raises(ValidationError):
            NativeOutcome.model_validate(fields)

        assert not (controller.directory / "native-result.json").exists()


@pytest.mark.parametrize(
    "target", ["/dev/disk1s1", "/dev/disk1;echo", "/private/disk1"]
)
def test_native_request_refuses_substituted_whole_device(
    tmp_path: Path, target: str
) -> None:
    with create(tmp_path / "target") as controller:
        fields = request_for(controller).model_dump(mode="json")
        fields["device"] = target

        with pytest.raises(ValidationError):
            NativeRequest.model_validate(fields)

        assert controller.manifest.native is None


@pytest.mark.parametrize(
    "defect", ["missing", "invalid", "oversized", "symlink", "stale", "phase", "helper"]
)
def test_settled_helper_without_matching_result_blocks_all_cleanup(
    tmp_path: Path, closed_helper: ProcessToken, defect: str
) -> None:
    with create(tmp_path / "unknown") as controller:
        request = request_for(controller).model_copy(update={"helper": closed_helper})
        bind_request(controller, request)
        sentinel = controller.directory / image_name()
        sentinel.write_bytes(b"untouched")
        path = controller.directory / "native-result.json"
        value = no_request(request).model_dump(mode="json")
        if defect == "invalid":
            save(path, b'{"version":true}')
        elif defect == "oversized":
            save(path, b"x" * 8193)
        elif defect == "symlink":
            path.symlink_to(sentinel)
        elif defect != "missing":
            if defect == "stale":
                value["request_id"] = "2" * 32
            elif defect == "phase":
                value["phase"] = "eject"
            else:
                value["helper"] = controller.manifest.controller.model_dump()
            save(path, json.dumps(value).encode())

        with pytest.raises(PressureFailure) as failure:
            controller.cleanup()

        assert failure.value.code.startswith("native_")
        assert sentinel.read_bytes() == b"untouched"
        assert not controller.manifest.detached
        assert not controller.manifest.resources_closed
        assert controller.manifest.native == request
        note = json.loads(failure.value.__notes__[0])
        assert note["result"] == "unknown"
        assert note["recorded_completion_known"] is False
        assert str(controller.directory) not in str(failure.value)
        assert str(closed_helper.pid) not in failure.value.__notes__[0]


def test_pending_before_helper_registration_never_invents_no_request(
    tmp_path: Path,
) -> None:
    with create(tmp_path / "unregistered") as controller:
        request = request_for(controller)
        bind_request(controller, request)
        save(controller.directory / "native-result.json", b"{}")

        with pytest.raises(PressureFailure, match="native_completion_unknown"):
            controller.settle_native()

        assert controller.manifest.native == request
        assert not controller.manifest.resources_closed


@pytest.mark.parametrize(
    "sidecar", ["native-result.json", "native-state.json", "unmount-status.bin"]
)
def test_legacy_owner_refuses_native_sidecar(tmp_path: Path, sidecar: str) -> None:
    with create(tmp_path / "legacy") as controller:
        controller.manifest = controller.manifest.model_copy(update={"version": 1})
        controller.persist()
        original = read_manifest(controller.directory / "pressure-manifest.json")
        save(controller.directory / sidecar, b"{}")

        with pytest.raises(PressureFailure, match="legacy_native_state"):
            read_manifest(controller.directory / "pressure-manifest.json")

        assert original.version == 1
        assert b'"native"' not in original.encoded()


@pytest.mark.parametrize("reason", ["activation", "expired", "controller"])
def test_real_helper_admission_failure_records_no_native_request(
    tmp_path: Path, closed_helper: ProcessToken, reason: str
) -> None:
    with create(tmp_path / "admission") as controller:
        request = request_for(controller)
        if reason == "expired":
            request = request.model_copy(update={"deadline": time.monotonic() - 1})
        elif reason == "controller":
            request = request.model_copy(update={"controller": closed_helper})
        bind_request(controller, request)
        controller.native_starting = True
        arguments = (
            sys.executable,
            "-m",
            "tests.plugins.storage.pressure_disk_arbitration",
            str(controller.directory),
        )
        activation = (
            "2" * 32 if reason == "activation" else request.request_id
        ).encode()

        try:
            with pytest.raises(PressureFailure, match="command_failed") as failure:
                controller.effect(arguments, activation=activation, private_output=True)
        finally:
            controller.native_starting = False
        controller.settle_native()

        settled = controller.manifest.native
        assert settled is not None
        assert settled.outcome is not None
        assert settled.outcome.result == "not_requested"
        assert settled.outcome.code is None
        assert settled.outcome.cf_closed
        assert settled.helper is not None
        assert process_token(settled.helper.pid) != settled.helper
        assert not controller.manifest.command_inflight
        assert controller.manifest.command_exit_status == 1
        assert failure.value.command_stderr
        assert str(failure.value) == "command_failed: command=python exit=1"
        assert not (controller.directory / "command-stderr.txt").exists()
        assert (controller.directory / "native-text.bin").read_bytes() == b""
        retained = tmp_path / "retained"
        retained.mkdir()
        preserve_owner(
            controller.directory / "pressure-manifest.json", retained, "owner"
        )
        assert {path.name for path in retained.iterdir()} == {"owner.manifest.json"}


def test_completed_result_is_committed_without_fabricating_command_status(
    tmp_path: Path, closed_helper: ProcessToken
) -> None:
    with create(tmp_path / "lost-response") as controller:
        request = request_for(controller).model_copy(update={"helper": closed_helper})
        bind_request(controller, request)
        controller.manifest = controller.manifest.model_copy(
            update={"command": closed_helper, "command_inflight": True}
        )
        controller.persist()
        save(
            controller.directory / "native-result.json",
            no_request(request).model_dump_json().encode(),
        )

        controller.settle_native()
        saved = read_manifest(controller.directory / "pressure-manifest.json")
        controller.settle_native()

        assert saved.native is not None
        assert saved.native.outcome == no_request(request)
        assert saved == controller.manifest
        assert saved.command_exit_status is None
        assert not saved.command_inflight
        assert not saved.detached
        assert not saved.resources_closed


@pytest.mark.parametrize(
    "state", ["not_requested", "pending", "invalid", "open_cf", "success", "refused"]
)
def test_native_call_only_publishes_known_closed_outcome(
    tmp_path: Path, closed_helper: ProcessToken, state: str
) -> None:
    with create(tmp_path / "state") as controller:
        request = request_for(controller).model_copy(update={"helper": closed_helper})
        call = NativeCall(request)
        call.submitted = state != "not_requested"
        call.finished = state in {"invalid", "open_cf", "success", "refused"}
        call.invalid = state == "invalid"
        call.cf_closed = state != "open_cf"
        call.refused = state == "refused"
        call.code = 49168 if call.refused else 0

        outcome = call.outcome()

        if state in {"pending", "invalid", "open_cf"}:
            assert outcome is None
        else:
            assert outcome is not None
            assert outcome.result == state
            assert outcome.cf_closed
            assert outcome.code == (None if state == "not_requested" else call.code)


def test_native_failure_note_is_fixed_and_remains_first_snapshot(
    tmp_path: Path,
) -> None:
    with create(tmp_path / "snapshot") as controller:
        request = request_for(controller)
        failure = PressureFailure("native_completion_unknown")

        failure.add_note(native_note(request))
        bind_request(controller, request.model_copy(update={"phase": "eject"}))

        note = failure.__notes__[0]
        assert len(note.encode()) <= 8192
        assert json.loads(note) == {
            "native_summary_version": 1,
            "phase": "unmount",
            "result": "unknown",
            "recorded_completion_known": False,
            "recorded_cf_closed": "unknown",
            "code": "unknown",
        }


@pytest.mark.parametrize("text", ["private", "x" * 10000, "\U0001f680" * 10000])
def test_private_cf_text_copy_is_bounded_before_conversion(
    tmp_path: Path, text: str
) -> None:
    if sys.platform != "darwin":
        with create(tmp_path / "platform") as controller:
            call = NativeCall(request_for(controller))

            with pytest.raises(PressureFailure, match="native_platform"):
                call.perform()

            assert call.outcome() is None
        return
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    ref = ctypes.c_void_p
    ffi = ctypes.CFUNCTYPE
    create_string = ffi(
        ref, ref, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32, ctypes.c_ubyte
    )(("CFStringCreateWithBytes", cf))
    release = ffi(None, ref)(("CFRelease", cf))
    encoded = text.encode()
    string = cast(int | None, create_string(None, encoded, len(encoded), 0x08000100, 0))
    assert string is not None
    try:
        copied, truncated = private_text(cf, string)
    finally:
        release(string)

    assert len(copied) <= 1024
    assert truncated == (len(text.encode("utf-16-le")) > 512)
    assert copied.decode() == (
        text[:128] if text.startswith("\U0001f680") else text[:256]
    )


def test_unregistered_real_helper_exit_is_not_native_completion(
    tmp_path: Path, closed_helper: ProcessToken
) -> None:
    with create(tmp_path / "mismatched-helper") as controller:
        request = request_for(controller).model_copy(update={"helper": closed_helper})
        bind_request(controller, request)
        arguments = (
            sys.executable,
            "-m",
            "tests.plugins.storage.pressure_disk_arbitration",
            str(controller.directory),
        )

        with pytest.raises(PressureFailure, match="command_failed"):
            controller.effect(
                arguments, activation=request.request_id.encode(), private_output=True
            )
        with pytest.raises(PressureFailure, match="native_completion_unknown"):
            controller.settle_native()

        assert not (controller.directory / "native-result.json").exists()
        assert controller.manifest.native == request
        token = controller.manifest.command
        assert token is not None
        assert token != request.helper
        assert process_token(token.pid) != token
        assert not controller.manifest.resources_closed


def test_expired_absolute_command_deadline_refuses_spawn_after_intent(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "never-started"
    arguments = (
        sys.executable,
        "-I",
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'started')",
        str(marker),
    )
    with create(tmp_path / "expired-command") as controller:
        cutoff = time.monotonic() - 1

        with pytest.raises(PressureFailure, match="command_deadline"):
            controller.effect(arguments, seconds=30, deadline=cutoff)

        assert not marker.exists()
        assert controller.manifest.command is None
        assert not controller.manifest.command_inflight
        assert controller.manifest.command_exit_status is None


def test_private_command_persistence_failure_keeps_rendered_chain_content_free(
    tmp_path: Path,
) -> None:
    with create(tmp_path / "private-diagnostic-root") as controller:
        pending = controller.directory / "native-command-stderr.pending"
        pending.symlink_to(controller.directory / "private-missing-target")
        arguments = (
            sys.executable,
            "-I",
            "-c",
            "import os; os.write(2,b'private-native-marker'); raise SystemExit(17)",
        )

        try:
            with pytest.raises(BaseExceptionGroup) as failure:
                controller.effect(arguments, private_output=True)
        finally:
            pending.unlink()

        assert len(failure.value.exceptions) == 2
        original, persistence = failure.value.exceptions
        assert isinstance(original, PressureFailure)
        assert original.code == "command_failed"
        assert original.command_stderr == b"private-native-marker"
        assert isinstance(persistence, PressureFailure)
        assert persistence.code == "native_diagnostic_persistence"
        rendered = "".join(traceback.format_exception(failure.value))
        assert str(controller.directory) not in rendered
        assert "private-native-marker" not in rendered
        assert "private-missing-target" not in rendered
        assert not controller.manifest.command_inflight
        assert controller.manifest.command_exit_status == 17


def test_matching_result_cannot_settle_while_actual_helper_is_live(
    tmp_path: Path,
) -> None:
    with create(tmp_path / "live-helper") as controller:
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-c",
                "import sys; print('ready',flush=True); sys.stdin.buffer.read(1)",
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], 5)
            assert ready
            assert process.stdout.readline(64) == b"ready\n"
            token = process_token(process.pid)
            assert token is not None
            request = request_for(controller).model_copy(update={"helper": token})
            bind_request(controller, request)
            save(
                controller.directory / "native-result.json",
                no_request(request).model_dump_json().encode(),
            )
            controller.native_deadline = time.monotonic() + 5.05

            with pytest.raises(PressureFailure) as failure:
                controller.settle_native()

            assert failure.value.code in {
                "child_closure_unresolved",
                "native_release_deadline",
            }
            assert process_token(token.pid) == token
            assert controller.manifest.native == request
            assert not controller.manifest.resources_closed
            output, errors = process.communicate(b"c", timeout=5)
            assert process.returncode == 0
            assert output == errors == b""
            controller.native_deadline = None
            controller.settle_native()
            assert controller.manifest.native is not None
            assert controller.manifest.native.outcome == no_request(request)
            assert not controller.manifest.detached
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
