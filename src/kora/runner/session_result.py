import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from kora.ir.vcs_ref_json import canonical_bytes
from kora.protocols.runtime import SESSION_OBSERVATION, SessionObservation
from kora.runner.filesystem import (
    DirectoryAnchor,
    create_directory,
    open_private_directory,
)
from kora.runner.registry import synchronize, unlink_if_present

RESULT_DIRECTORY: Final[str] = "result"
RESULT_NAME: Final[str] = "session.json"
MAX_RESULT_BYTES: Final[int] = 16 * 1_048_576
CREATE_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
READ_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class ResultTooLarge(Exception):
    def __init__(self) -> None:
        super().__init__("the session observation exceeds its size cap")


class ResultUnreadable(Exception):
    def __init__(self) -> None:
        super().__init__("the session observation does not match its record")


@dataclass(frozen=True)
class ResultFile:
    name: str
    size: int
    digest: str


def digest_of(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def result_directory(control: DirectoryAnchor) -> DirectoryAnchor:
    try:
        return create_directory(control, RESULT_DIRECTORY)
    except FileExistsError:
        return open_private_directory(control, RESULT_DIRECTORY)


def write_observation(
    control: DirectoryAnchor, observation: SessionObservation
) -> ResultFile:
    payload = canonical_bytes(observation.model_dump(mode="json"))
    if len(payload) > MAX_RESULT_BYTES:
        raise ResultTooLarge
    directory = result_directory(control)
    try:
        descriptor = os.open(
            RESULT_NAME, CREATE_FLAGS, 0o400, dir_fd=directory.descriptor
        )
        try:
            try:
                written_through(descriptor, payload)
            finally:
                os.close(descriptor)
            synchronize(directory.descriptor)
        except OSError:
            unlink_if_present(directory, RESULT_NAME)
            raise
    finally:
        directory.close()
    return ResultFile(RESULT_NAME, len(payload), digest_of(payload))


def written_through(descriptor: int, payload: bytes) -> None:
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
    if reference.name != RESULT_NAME or not 0 < reference.size <= MAX_RESULT_BYTES:
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
    try:
        return SESSION_OBSERVATION.validate_json(payload)
    except ValidationError:
        raise ResultUnreadable from None


def bounded_read(descriptor: int, size: int) -> bytes:
    payload = bytearray()
    while len(payload) <= size:
        chunk = os.read(descriptor, size + 1 - len(payload))
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)
