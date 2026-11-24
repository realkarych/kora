import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Final, Literal

import pytest

from kora.ir.vcs_ref_json import canonical_bytes
from kora.protocols.runtime import SessionFailedObservation
from kora.runner.registry import ControlFileRef, Submitter
from kora.runner.session_result import (
    FINALIZATION_NAME,
    RESULT_DIRECTORY,
    RESULT_NAME,
    FinalizationCheckpoint,
    FinalizationFacts,
    FinalizationImplementation,
    FinalizationReferences,
    ResultFile,
    ResultUnreadable,
    digest_of,
    prepare_observation,
    read_finalization,
    read_observation,
    write_observation,
)
from kora.runner.session_result_encoding import MAX_FINALIZATION_BYTES
from tests.runner.agent_support import private_directory
from tests.support.local_peers import identity_of

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

    reference = write_observation(
        control, prepare_observation(OBSERVATION, (), Event())
    )
    observation = read_observation(control, reference)
    control.close()

    assert observation == OBSERVATION
    assert reference.name == RESULT_NAME


@pytest.mark.parametrize("tampering", sorted(TAMPERING))
def test_session_result_read_refuses_tampering(tmp_path: Path, tampering: str) -> None:
    control = private_directory(tmp_path / "control")
    written = write_observation(control, prepare_observation(OBSERVATION, (), Event()))
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
        write_observation(control, prepare_observation(OBSERVATION, (), Event()))
    control.close()

    assert (directory / RESULT_NAME).read_bytes() == b"kept"
    assert target.read_bytes() == b"kept"


def finalization_facts() -> FinalizationFacts:
    process = identity_of(os.getpid())
    return FinalizationFacts(
        attempt_id="attempt-1",
        execution_domain_id="domain-1",
        submitter=Submitter(dispatcher_id="dispatcher-1", process=process),
        supervisor=process,
        request_digest=digest_of(b"request"),
        session_spec_digest=digest_of(b"session spec"),
        implementation=FinalizationImplementation(name="fake", version="fake-1"),
        capture="unavailable",
        duration_ms=17,
        price_table_id="price-1",
    )


def sidecar_case() -> FinalizationCheckpoint:
    return FinalizationCheckpoint(
        format="kora.local.session-finalization/1",
        checked_result=ControlFileRef(
            name=RESULT_NAME, size=7, digest=digest_of(b"result!")
        ),
        finalization=finalization_facts(),
    )


@pytest.mark.parametrize(
    "case",
    [
        "positive",
        "unknown_version",
        "duplicate",
        "missing_member",
        "noncanonical",
        "oversized",
        "foreign_result",
        "wrong_sidecar_name",
        "link",
        "wrong_sidecar_digest",
    ],
)
def test_session_finalization_reads_only_the_bound_canonical_sidecar(
    tmp_path: Path,
    case: Literal[
        "positive",
        "unknown_version",
        "duplicate",
        "missing_member",
        "noncanonical",
        "oversized",
        "foreign_result",
        "wrong_sidecar_name",
        "link",
        "wrong_sidecar_digest",
    ],
) -> None:
    control = private_directory(tmp_path / "control")
    checkpoint = sidecar_case()
    payload = canonical_bytes(checkpoint.model_dump(mode="json"))
    result_ref = checkpoint.checked_result
    sibling_name = FINALIZATION_NAME
    if case == "unknown_version":
        payload = payload.replace(b"session-finalization/1", b"session-finalization/2")
    elif case == "duplicate":
        payload = payload.replace(
            b'"capture":"unavailable"',
            b'"capture":"unavailable","capture":"unavailable"',
        )
    elif case == "missing_member":
        payload = payload.replace(b'"name":"fake",', b"")
    elif case == "noncanonical":
        payload += b"\n"
    elif case == "oversized":
        payload += b" " * (MAX_FINALIZATION_BYTES + 1 - len(payload))
    elif case == "foreign_result":
        result_ref = result_ref.model_copy(update={"digest": OTHER_DIGEST})
    elif case == "wrong_sidecar_name":
        sibling_name = "session.foreign.json"
    directory = tmp_path / "control" / RESULT_DIRECTORY
    directory.mkdir(mode=0o700)
    file = directory / FINALIZATION_NAME
    file.write_bytes(payload)
    if case == "link":
        os.link(file, tmp_path / "link")
    references = FinalizationReferences(
        checked_result=result_ref,
        finalization=ControlFileRef(
            name=sibling_name,
            size=len(payload),
            digest=OTHER_DIGEST
            if case == "wrong_sidecar_digest"
            else digest_of(payload),
        ),
    )

    try:
        if case == "positive":
            observation = read_finalization(control, references)
            assert observation == checkpoint.finalization
        else:
            with pytest.raises(ResultUnreadable):
                read_finalization(control, references)
    finally:
        control.close()
