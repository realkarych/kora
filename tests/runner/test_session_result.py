import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from kora.protocols.runtime import SessionFailedObservation
from kora.runner.session_result import (
    RESULT_DIRECTORY,
    RESULT_NAME,
    ResultFile,
    ResultUnreadable,
    read_observation,
    write_observation,
)
from tests.runner.agent_support import private_directory

OBSERVATION: Final[SessionFailedObservation] = SessionFailedObservation(
    kind="session_failed", code="script_end_invalid"
)
OTHER_DIGEST: Final[str] = "sha256:" + "0" * 64


def replaced_by_symlink(path: Path, reference: ResultFile) -> ResultFile:
    target = path.parent / "target.json"
    path.rename(target)
    path.symlink_to(target)
    return reference


def replaced_by_fifo(path: Path, reference: ResultFile) -> ResultFile:
    path.unlink()
    os.mkfifo(path, 0o600)
    return reference


def hard_linked(path: Path, reference: ResultFile) -> ResultFile:
    os.link(path, path.parent.parent / "alias.json")
    return reference


def resized(path: Path, reference: ResultFile) -> ResultFile:
    return replace(reference, size=reference.size + 1)


def redigested(path: Path, reference: ResultFile) -> ResultFile:
    return replace(reference, digest=OTHER_DIGEST)


def renamed(path: Path, reference: ResultFile) -> ResultFile:
    return replace(reference, name="other.json")


TAMPERING: Final[dict[str, Callable[[Path, ResultFile], ResultFile]]] = {
    "symlink": replaced_by_symlink,
    "fifo": replaced_by_fifo,
    "hard_link": hard_linked,
    "size": resized,
    "digest": redigested,
    "name": renamed,
}


def test_session_result_round_trip(tmp_path: Path) -> None:
    control = private_directory(tmp_path / "control")

    reference = write_observation(control, OBSERVATION)
    observation = read_observation(control, reference)
    control.close()

    assert observation == OBSERVATION
    assert reference.name == RESULT_NAME


@pytest.mark.parametrize("tampering", sorted(TAMPERING))
def test_session_result_read_refuses_tampering(tmp_path: Path, tampering: str) -> None:
    control = private_directory(tmp_path / "control")
    written = write_observation(control, OBSERVATION)
    path = tmp_path / "control" / RESULT_DIRECTORY / RESULT_NAME
    reference = TAMPERING[tampering](path, written)

    with pytest.raises(ResultUnreadable):
        read_observation(control, reference)
    control.close()


@pytest.mark.parametrize("occupant", ["file", "symlink"])
def test_session_result_create_refuses_existing_name(
    tmp_path: Path, occupant: str
) -> None:
    control = private_directory(tmp_path / "control")
    directory = tmp_path / "control" / RESULT_DIRECTORY
    directory.mkdir(mode=0o700)
    target = tmp_path / "target.json"
    target.write_bytes(b"kept")
    if occupant == "file":
        (directory / RESULT_NAME).write_bytes(b"kept")
    else:
        (directory / RESULT_NAME).symlink_to(target)

    with pytest.raises(FileExistsError):
        write_observation(control, OBSERVATION)
    control.close()

    assert (directory / RESULT_NAME).read_bytes() == b"kept"
    assert target.read_bytes() == b"kept"
