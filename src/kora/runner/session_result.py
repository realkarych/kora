import hashlib
import os
import stat
from dataclasses import dataclass, replace
from threading import Event
from typing import Final, Literal

from pydantic import ValidationError

from kora.ir.cost import Identifier as CostIdentifier
from kora.ir.scalars import Digest
from kora.ir.vcs_ref_json import JsonRefusal, canonical_bytes, decode_strict_json
from kora.protocols.boundary import BoundaryModel
from kora.protocols.harness import RunnerCause
from kora.protocols.identity import Count, Identifier, ProcessIdentity
from kora.protocols.runtime import (
    SESSION_OBSERVATION,
    SessionFailedObservation,
    SessionObservation,
)
from kora.runner.filesystem import (
    DirectoryAnchor,
    create_directory,
    open_private_directory,
)
from kora.runner.registry import (
    ControlFileRef,
    Submitter,
    synchronize,
    unlink_if_present,
)
from kora.runner.session_result_encoding import (
    MAX_FINALIZATION_BYTES,
    MAX_RESULT_BYTES,
    ResultContainsSecret,
    ResultEncoder,
    ResultEncoderFactory,
    ResultEncodingRefused,
    ResultTooLarge,
    check_running,
    encode_finalization,
    encode_observation,
)

RESULT_DIRECTORY: Final[str] = "result"
LEGACY_RESULT_NAME: Final[str] = "session.json"
RESULT_NAME: Final[str] = "session.checked.json"
FINALIZATION_NAME: Final[str] = "session.finalization.checked.json"
CREATE_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
READ_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class ResultUnreadable(Exception):
    def __init__(self) -> None:
        super().__init__("the session observation does not match its record")


@dataclass(frozen=True)
class ResultFile:
    name: str
    size: int
    digest: str


class FinalizationOrigin(BoundaryModel):
    attempt_id: Identifier
    execution_domain_id: Identifier
    submitter: Submitter
    supervisor: ProcessIdentity
    request_digest: Digest


class FinalizationImplementation(BoundaryModel):
    name: Literal["fake"]
    version: Literal["fake-1"]


class FinalizationFacts(FinalizationOrigin):
    session_spec_digest: Digest
    implementation: FinalizationImplementation
    capture: Literal["unavailable"]
    duration_ms: Count
    price_table_id: CostIdentifier

    def origin(self) -> FinalizationOrigin:
        return FinalizationOrigin(
            attempt_id=self.attempt_id,
            execution_domain_id=self.execution_domain_id,
            submitter=self.submitter,
            supervisor=self.supervisor,
            request_digest=self.request_digest,
        )


class FinalizationReferences(BoundaryModel):
    checked_result: ControlFileRef
    finalization: ControlFileRef


class FinalizationCheckpoint(BoundaryModel):
    format: Literal["kora.local.session-finalization/1"]
    checked_result: ControlFileRef
    finalization: FinalizationFacts


@dataclass(frozen=True)
class PreparedFinalization:
    payload: bytearray
    reference: ResultFile


@dataclass(frozen=True)
class PreparedObservation:
    payload: bytearray
    reference: ResultFile
    failure: str | None
    cause: RunnerCause | None
    stop_reason: str | None
    journal_complete: bool
    finalization: PreparedFinalization | None = None


def prepare_observation(
    observation: SessionObservation,
    forms: tuple[str, ...],
    stop: Event,
    *,
    encoder_factory: ResultEncoderFactory = ResultEncoder,
) -> PreparedObservation:
    try:
        payload = encode_observation(observation, forms, stop, encoder_factory)
    except ResultContainsSecret:
        refusal = "secret_leak"
    except ResultTooLarge:
        refusal = "result_too_large"
    except ResultEncodingRefused:
        refusal = "result_unwritten"
    else:
        return summarize_observation(observation, payload, stop)
    return prepare_failure(refusal, stop)


def summarize_observation(
    observation: SessionObservation, payload: bytearray, stop: Event
) -> PreparedObservation:
    reference = result_reference(payload, stop)
    if isinstance(observation, SessionFailedObservation):
        return PreparedObservation(
            payload, reference, observation.code, None, None, False
        )
    return PreparedObservation(
        payload,
        reference,
        None,
        observation.cause,
        observation.result.stop_reason,
        observation.journal_complete,
    )


def prepare_failure(code: str, stop: Event) -> PreparedObservation:
    observation = SessionFailedObservation(kind="session_failed", code=code)
    payload = encode_observation(observation, (), stop)
    return PreparedObservation(
        payload, result_reference(payload, stop), code, None, None, False
    )


def prepare_finalization(
    prepared: PreparedObservation,
    facts: FinalizationFacts,
    forms: tuple[str, ...],
    stop: Event,
) -> PreparedObservation:
    if prepared.failure is not None:
        return prepared
    reference = prepared.reference
    checkpoint = FinalizationCheckpoint(
        format="kora.local.session-finalization/1",
        checked_result=ControlFileRef(
            name=reference.name, size=reference.size, digest=reference.digest
        ),
        finalization=facts,
    )
    try:
        payload = encode_finalization(checkpoint, forms, stop)
        sibling = result_reference(payload, stop, FINALIZATION_NAME)
    except (
        ResultContainsSecret,
        ResultTooLarge,
        ResultEncodingRefused,
    ):
        return prepared
    return replace(prepared, finalization=PreparedFinalization(payload, sibling))


def result_reference(
    payload: bytearray, stop: Event, name: str = RESULT_NAME
) -> ResultFile:
    digest = hashlib.sha256()
    view = memoryview(payload)
    for offset in range(0, len(view), 16_384):
        check_running(stop)
        digest.update(view[offset : offset + 16_384])
    check_running(stop)
    return ResultFile(name, len(payload), f"sha256:{digest.hexdigest()}")


def digest_of(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def result_directory(control: DirectoryAnchor) -> DirectoryAnchor:
    try:
        return create_directory(control, RESULT_DIRECTORY)
    except FileExistsError:
        return open_private_directory(control, RESULT_DIRECTORY)


def write_observation(
    control: DirectoryAnchor, prepared: PreparedObservation
) -> ResultFile:
    return write_result(control, prepared.reference, prepared.payload)


def write_finalization(
    control: DirectoryAnchor, prepared: PreparedFinalization
) -> ResultFile:
    return write_result(control, prepared.reference, prepared.payload)


def write_result(
    control: DirectoryAnchor, reference: ResultFile, payload: bytearray
) -> ResultFile:
    directory = result_directory(control)
    try:
        descriptor = os.open(
            reference.name, CREATE_FLAGS, 0o400, dir_fd=directory.descriptor
        )
        try:
            try:
                written_through(descriptor, payload)
            finally:
                os.close(descriptor)
            synchronize(directory.descriptor)
        except OSError:
            unlink_if_present(directory, reference.name)
            raise
    finally:
        directory.close()
    return reference


def written_through(descriptor: int, payload: bytearray) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view) :]
    synchronize(descriptor)


def discard_observation(control: DirectoryAnchor) -> None:
    directory = result_directory(control)
    try:
        unlink_if_present(directory, RESULT_NAME)
        synchronize(directory.descriptor)
    finally:
        directory.close()


def read_observation(
    control: DirectoryAnchor, reference: ResultFile
) -> SessionObservation:
    payload = read_result(control, reference, RESULT_NAME, MAX_RESULT_BYTES)
    try:
        return SESSION_OBSERVATION.validate_json(payload)
    except ValidationError:
        raise ResultUnreadable from None


def read_finalization(
    control: DirectoryAnchor, references: FinalizationReferences
) -> FinalizationFacts:
    reference = references.finalization
    payload = read_result(
        control,
        ResultFile(reference.name, reference.size, reference.digest),
        FINALIZATION_NAME,
        MAX_FINALIZATION_BYTES,
    )
    value = decode_strict_json(payload, MAX_FINALIZATION_BYTES, 16)
    if isinstance(value, JsonRefusal) or not isinstance(value, dict):
        raise ResultUnreadable
    if canonical_bytes(value) != payload:
        raise ResultUnreadable
    try:
        checkpoint = FinalizationCheckpoint.model_validate(value)
    except ValidationError:
        raise ResultUnreadable from None
    if (
        references.checked_result.name != RESULT_NAME
        or not 0 < references.checked_result.size <= MAX_RESULT_BYTES
        or checkpoint.checked_result != references.checked_result
    ):
        raise ResultUnreadable
    return checkpoint.finalization


def read_result(
    control: DirectoryAnchor, reference: ResultFile, name: str, limit: int
) -> bytes:
    if reference.name != name or not 0 < reference.size <= limit:
        raise ResultUnreadable
    directory = open_private_directory(control, RESULT_DIRECTORY)
    try:
        descriptor = os.open(reference.name, READ_FLAGS, dir_fd=directory.descriptor)
    except OSError:
        raise ResultUnreadable from None
    finally:
        directory.close()
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_size != reference.size
        ):
            raise ResultUnreadable
        payload = bounded_read(descriptor, reference.size)
    finally:
        os.close(descriptor)
    if len(payload) != reference.size or digest_of(payload) != reference.digest:
        raise ResultUnreadable
    return payload


def bounded_read(descriptor: int, size: int) -> bytes:
    payload = bytearray()
    while len(payload) <= size:
        chunk = os.read(descriptor, size + 1 - len(payload))
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)
