import hashlib
import os
from typing import Final

from pydantic import ValidationError

from kora.ir.json import JsonObject
from kora.plugins.runtime.local import capture
from kora.plugins.runtime.local.channel import (
    SECTION_FRAME_BYTES,
    LogName,
    delivery_error,
)
from kora.plugins.runtime.local.workspace import PreparedWorkspace
from kora.protocols.runtime import MAX_TAIL_BYTES, CaptureFacts, CommandObservation
from kora.runner.filesystem import DirectoryAnchor

COMMAND_OBSERVED: Final[str] = "runner.command_observed"
NANOSECONDS_PER_MILLISECOND: Final[int] = 1_000_000


def recorded_stream(facts: capture.CaptureFacts | None) -> CaptureFacts | None:
    if facts is None or facts.identity is None:
        return None
    return CaptureFacts(
        payload_digest=facts.payload_digest,
        size=facts.size,
        observed_bytes=facts.observed_bytes,
        eof=facts.eof,
        truncated=facts.truncated,
        tail=b"",
    )


def completed_observation(
    exit_code: int,
    elapsed_ns: int,
    prepared: PreparedWorkspace,
    stdout: capture.CaptureFacts | None,
    stderr: capture.CaptureFacts | None,
) -> CommandObservation | None:
    out, err = recorded_stream(stdout), recorded_stream(stderr)
    if out is None or err is None:
        return None
    try:
        return CommandObservation(
            exit_code=exit_code,
            signal=-exit_code if exit_code < 0 else None,
            termination="none",
            stop_reason="completed",
            duration_ms=max(0, elapsed_ns) // NANOSECONDS_PER_MILLISECOND,
            repo=prepared.repo,
            commit=prepared.commit,
            base_commit=prepared.base_commit,
            protected_paths=(),
            protection="unprotected",
            resources=(),
            resource_set_digest=None,
            resource_protection="none",
            stdout=out,
            stderr=err,
        )
    except ValidationError:
        return None


def observation_record(observation: CommandObservation) -> JsonObject:
    return observation.model_dump(
        mode="json", exclude={"stdout": {"tail"}, "stderr": {"tail"}}
    )


def sealed_tail(
    attempt: DirectoryAnchor,
    which: LogName,
    facts: capture.CaptureFacts | None,
    recorded: CaptureFacts,
) -> bytes:
    if facts is None:
        raise delivery_error("integrity_error")
    opened = capture.open_log(attempt, which, facts, 0)
    digest = hashlib.sha256()
    tail = b""
    try:
        for position in range(0, opened.size, SECTION_FRAME_BYTES):
            length = min(SECTION_FRAME_BYTES, opened.size - position)
            piece = capture.read_log_frame(opened, position, length)
            digest.update(piece)
            tail = (tail + piece)[-MAX_TAIL_BYTES:]
    finally:
        os.close(opened.descriptor)
    if "sha256:" + digest.hexdigest() != recorded.payload_digest:
        raise delivery_error("integrity_error")
    return tail


def with_tails(
    observation: CommandObservation, stdout: bytes, stderr: bytes
) -> CommandObservation:
    return CommandObservation.model_validate(
        {
            **observation.model_dump(),
            "stdout": {**observation.stdout.model_dump(), "tail": stdout},
            "stderr": {**observation.stderr.model_dump(), "tail": stderr},
        }
    )
