import hashlib
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from kora.protocols.time import Clock

MAX_PATH_BYTES: Final[int] = 4096
MAX_PATH_COMPONENTS: Final[int] = 32
MAX_EXECUTABLE_BYTES: Final[int] = 512 * 1024 * 1024
HASH_CHUNK_BYTES: Final[int] = 256 * 1024
DIRECTORY_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
EXECUTABLE_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
UNTRUSTED_WRITE: Final[int] = stat.S_IWGRP | stat.S_IWOTH
PRIVILEGED_EXECUTION: Final[int] = stat.S_ISUID | stat.S_ISGID


class ExecutableFailure(StrEnum):
    LOCATION = "installed_executable_location"
    PROTECTION = "installed_executable_protection"
    IDENTITY = "installed_executable_identity"
    SIZE = "installed_executable_size"
    DIGEST = "installed_executable_digest"
    DEADLINE = "installed_executable_deadline"


class ExecutableVerificationError(ValueError):
    def __init__(self, reason: ExecutableFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class ExecutablePin:
    size: int
    digest: str

    def __post_init__(self) -> None:
        if not 0 < self.size <= MAX_EXECUTABLE_BYTES:
            raise ExecutableVerificationError(ExecutableFailure.SIZE)
        if len(self.digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.digest
        ):
            raise ExecutableVerificationError(ExecutableFailure.DIGEST)


@dataclass(frozen=True)
class InstalledExecutable:
    path: Path
    device: int
    inode: int


def verify_installed_executable(
    path: Path, pin: ExecutablePin, deadline_ns: int, clock: Clock
) -> InstalledExecutable:
    parts = path.parts[1:]
    if (
        path.anchor != "/"
        or not 0 < len(parts) <= MAX_PATH_COMPONENTS
        or len(os.fsencode(path)) > MAX_PATH_BYTES
        or any(part == ".." or "\0" in part for part in parts)
    ):
        raise ExecutableVerificationError(ExecutableFailure.LOCATION)
    try:
        with ExitStack() as owned:
            _require_time(clock, deadline_ns)
            parent = os.open("/", DIRECTORY_FLAGS)
            owned.callback(os.close, parent)
            _require_protection(os.fstat(parent))
            for component in parts[:-1]:
                _require_time(clock, deadline_ns)
                parent = os.open(component, DIRECTORY_FLAGS, dir_fd=parent)
                owned.callback(os.close, parent)
                _require_protection(os.fstat(parent))
            _require_time(clock, deadline_ns)
            name = parts[-1]
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            check_executable_entry(entry)
            if not os.access(
                name,
                os.X_OK,
                dir_fd=parent,
                effective_ids=True,
                follow_symlinks=False,
            ):
                raise ExecutableVerificationError(ExecutableFailure.PROTECTION)
            _require_time(clock, deadline_ns)
            descriptor = os.open(name, EXECUTABLE_FLAGS, dir_fd=parent)
            owned.callback(os.close, descriptor)
            verify_executable_contents(descriptor, entry, pin, deadline_ns, clock)
            if _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _stamp(
                entry
            ):
                raise ExecutableVerificationError(ExecutableFailure.IDENTITY)
            _require_time(clock, deadline_ns)
        _require_time(clock, deadline_ns)
    except OSError:
        raise ExecutableVerificationError(ExecutableFailure.LOCATION) from None
    return InstalledExecutable(path, entry.st_dev, entry.st_ino)


def _require_protection(entry: os.stat_result) -> None:
    if entry.st_uid != 0 or entry.st_mode & UNTRUSTED_WRITE:
        raise ExecutableVerificationError(ExecutableFailure.PROTECTION)


def check_executable_entry(entry: os.stat_result) -> None:
    if not stat.S_ISREG(entry.st_mode):
        raise ExecutableVerificationError(ExecutableFailure.LOCATION)
    _require_protection(entry)
    if entry.st_mode & PRIVILEGED_EXECUTION:
        raise ExecutableVerificationError(ExecutableFailure.PROTECTION)


def _stamp(entry: os.stat_result) -> tuple[int, ...]:
    return (
        entry.st_dev,
        entry.st_ino,
        entry.st_mode,
        entry.st_uid,
        entry.st_gid,
        entry.st_nlink,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    )


def _require_time(clock: Clock, deadline_ns: int) -> None:
    if clock.monotonic_ns() >= deadline_ns:
        raise ExecutableVerificationError(ExecutableFailure.DEADLINE)


def verify_executable_contents(
    descriptor: int,
    entry: os.stat_result,
    pin: ExecutablePin,
    deadline_ns: int,
    clock: Clock,
) -> None:
    _require_time(clock, deadline_ns)
    stamp = _stamp(entry)
    if _stamp(os.fstat(descriptor)) != stamp:
        raise ExecutableVerificationError(ExecutableFailure.IDENTITY)
    if entry.st_size != pin.size:
        raise ExecutableVerificationError(ExecutableFailure.SIZE)
    digest = hashlib.sha256()
    offset = 0
    while offset < pin.size:
        _require_time(clock, deadline_ns)
        chunk = os.pread(descriptor, min(HASH_CHUNK_BYTES, pin.size - offset), offset)
        if not chunk:
            raise ExecutableVerificationError(ExecutableFailure.SIZE)
        digest.update(chunk)
        offset += len(chunk)
    _require_time(clock, deadline_ns)
    if os.pread(descriptor, 1, offset):
        raise ExecutableVerificationError(ExecutableFailure.SIZE)
    if _stamp(os.fstat(descriptor)) != stamp:
        raise ExecutableVerificationError(ExecutableFailure.IDENTITY)
    _require_time(clock, deadline_ns)
    if digest.hexdigest() != pin.digest:
        raise ExecutableVerificationError(ExecutableFailure.DIGEST)
