import hashlib
import os
from collections.abc import Iterator
from typing import Final

from pydantic import ValidationError

from kora.ir.vcs_ref_json import JsonRefusal, decode_strict_json
from kora.plugins.runtime.local.channel import (
    SECTION_FRAME_BYTES,
    LogName,
    delivery_error,
)
from kora.plugins.runtime.local.observation import COMMAND_OBSERVED
from kora.protocols.identity import ProcessIdentity
from kora.protocols.runtime import MAX_TAIL_BYTES, CaptureFacts, CommandObservation
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_private_directory,
    open_private_file,
)
from kora.runner.identity import IdentityReader, IdentityState
from kora.runner.journal import JOURNAL_NAME, RECORD_DEPTH
from kora.runner.registry import (
    RESULT_FILE_BYTES,
    CompletedResult,
    ControlFileRef,
    JournalRecordRef,
)

RESULT_DIRECTORY: Final[str] = "result"
CONTROL_DIRECTORY: Final[str] = "control"


def recorded_exited(reader: IdentityReader, recorded: ProcessIdentity) -> bool:
    try:
        observation = reader.read(recorded.pid)
    except OSError:
        return False
    if observation.state is IdentityState.ABSENT:
        return True
    process = observation.process
    if observation.state is not IdentityState.PRESENT or process is None:
        return False
    return process.dead or process.identity.start_time != recorded.start_time


def digest_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def exact_read(descriptor: int, offset: int, length: int) -> bytes:
    try:
        payload = os.pread(descriptor, length, offset)
    except OSError:
        raise delivery_error("integrity_error") from None
    if len(payload) != length:
        raise delivery_error("integrity_error")
    return payload


def opened(attempt: DirectoryAnchor, name: str) -> int:
    try:
        return open_private_file(attempt, name, os.O_RDONLY)
    except (OSError, FilesystemError):
        raise delivery_error("transfer_refused") from None


def journal_record(attempt: DirectoryAnchor, ref: JournalRecordRef) -> bytes:
    descriptor = opened(attempt, JOURNAL_NAME)
    try:
        payload = exact_read(descriptor, ref.offset, ref.length)
    finally:
        os.close(descriptor)
    if digest_of(payload) != ref.digest:
        raise delivery_error("integrity_error")
    return payload


def control_file(attempt: DirectoryAnchor, ref: ControlFileRef) -> bytes:
    try:
        control = open_private_directory(attempt, CONTROL_DIRECTORY)
        try:
            results = open_private_directory(control, RESULT_DIRECTORY)
        finally:
            control.close()
    except (OSError, FilesystemError):
        raise delivery_error("transfer_refused") from None
    try:
        descriptor = opened(results, ref.name)
    finally:
        results.close()
    try:
        if os.fstat(descriptor).st_size != ref.size or ref.size > RESULT_FILE_BYTES:
            raise delivery_error("integrity_error")
        payload = exact_read(descriptor, 0, ref.size)
    finally:
        os.close(descriptor)
    if digest_of(payload) != ref.digest:
        raise delivery_error("integrity_error")
    return payload


def result_payload(attempt: DirectoryAnchor, result: CompletedResult) -> bytes:
    if isinstance(result.ref, JournalRecordRef):
        return journal_record(attempt, result.ref)
    return control_file(attempt, result.ref)


def recorded_record(
    attempt: DirectoryAnchor, result: CompletedResult | None
) -> CommandObservation:
    if result is None or result.kind != "command":
        raise delivery_error("command_unobserved")
    line = result_payload(attempt, result)
    decoded = decode_strict_json(line.rstrip(b"\n"), len(line), RECORD_DEPTH)
    if (
        isinstance(decoded, JsonRefusal)
        or not isinstance(decoded, dict)
        or decoded.get("kind") != COMMAND_OBSERVED
    ):
        raise delivery_error("integrity_error")
    data = decoded.get("data")
    if not isinstance(data, dict):
        raise delivery_error("integrity_error")
    streams: dict[str, object] = {}
    for which in ("stdout", "stderr"):
        stream = data.get(which)
        if not isinstance(stream, dict):
            raise delivery_error("integrity_error")
        streams[which] = {**stream, "tail": b""}
    try:
        return CommandObservation.model_validate({**data, **streams})
    except ValidationError:
        raise delivery_error("integrity_error") from None


def recorded_observation(
    attempt: DirectoryAnchor, result: CompletedResult | None
) -> CommandObservation:
    record = recorded_record(attempt, result)
    stdout = log_tail(attempt, "stdout", record.stdout)
    stderr = log_tail(attempt, "stderr", record.stderr)
    try:
        return CommandObservation.model_validate(
            {
                **record.model_dump(),
                "stdout": {**record.stdout.model_dump(), "tail": stdout},
                "stderr": {**record.stderr.model_dump(), "tail": stderr},
            }
        )
    except ValidationError:
        raise delivery_error("integrity_error") from None


def log_frames(descriptor: int, facts: CaptureFacts) -> Iterator[bytes]:
    for position in range(0, facts.size, SECTION_FRAME_BYTES):
        yield exact_read(
            descriptor, position, min(SECTION_FRAME_BYTES, facts.size - position)
        )


def log_tail(attempt: DirectoryAnchor, which: LogName, facts: CaptureFacts) -> bytes:
    descriptor = opened(attempt, f"{which}.log")
    digest = hashlib.sha256()
    tail = b""
    try:
        for frame in log_frames(descriptor, facts):
            digest.update(frame)
            tail = (tail + frame)[-MAX_TAIL_BYTES:]
    finally:
        os.close(descriptor)
    if "sha256:" + digest.hexdigest() != facts.payload_digest:
        raise delivery_error("integrity_error")
    return tail


def copied_log(
    attempt: DirectoryAnchor, which: LogName, facts: CaptureFacts, spool: int
) -> None:
    descriptor = opened(attempt, f"{which}.log")
    digest = hashlib.sha256()
    try:
        for frame in log_frames(descriptor, facts):
            digest.update(frame)
            view = memoryview(frame)
            while view:
                view = view[os.write(spool, view) :]
    finally:
        os.close(descriptor)
    if "sha256:" + digest.hexdigest() != facts.payload_digest:
        raise delivery_error("integrity_error")


def attempt_token(attempt: DirectoryAnchor) -> str:
    try:
        descriptor = open_private_file(attempt, "attempt.token", os.O_RDONLY)
    except (OSError, FilesystemError):
        raise delivery_error("transfer_refused") from None
    try:
        raw = os.read(descriptor, 33)
    except OSError:
        raise delivery_error("transfer_refused") from None
    finally:
        os.close(descriptor)
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError:
        raise delivery_error("transfer_refused") from None
    if len(token) != 32:
        raise delivery_error("transfer_refused")
    return token
