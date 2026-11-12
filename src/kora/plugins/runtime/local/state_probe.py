import enum
import os
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path

from kora.plugins.runtime.local.configuration import LocalRuntimeConfiguration
from kora.plugins.runtime.local.runtime import anchored_root
from kora.runner.filesystem import DirectoryAnchor, FilesystemError, open_private_file

PROBE_PREFIX: str = ".kora-doctor-"
PROBE_FLAGS: int = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
PROBE_CONTENT: bytes = b"k"


class StateRootFailure(enum.StrEnum):
    UNSAFE = "state_root_unsafe"
    NOT_WRITABLE = "not_writable"


def remove_probe(anchor: DirectoryAnchor, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=anchor.descriptor)
    except OSError:
        return False
    return True


def write_probe(anchor: DirectoryAnchor) -> StateRootFailure | None:
    name = f"{PROBE_PREFIX}{secrets.token_hex(16)}"
    try:
        anchor.verify()
    except (OSError, FilesystemError):
        return StateRootFailure.UNSAFE
    try:
        descriptor = open_private_file(anchor, name, PROBE_FLAGS)
    except FilesystemError:
        remove_probe(anchor, name)
        return StateRootFailure.NOT_WRITABLE
    except OSError:
        return StateRootFailure.NOT_WRITABLE
    try:
        written = os.write(descriptor, PROBE_CONTENT)
    except OSError:
        written = 0
    finally:
        os.close(descriptor)
    removed = remove_probe(anchor, name)
    if written != len(PROBE_CONTENT) or not removed:
        return StateRootFailure.NOT_WRITABLE
    return None


def probe_state_root(
    environment: Mapping[str, str], home: Callable[[], Path]
) -> Path | StateRootFailure:
    anchored = anchored_root(
        LocalRuntimeConfiguration(state_root=None), environment, home
    )
    if anchored is None:
        return StateRootFailure.UNSAFE
    path, anchor = anchored
    try:
        failure = write_probe(anchor)
    finally:
        anchor.close()
    return path if failure is None else failure
