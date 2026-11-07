import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import publish_request, run
from kora.executor.failures import ExecutorError, Phase
from kora.ir.node import ErrorClass
from kora.plugins.vcs.fake import store
from kora.protocols.errors import KoraProtocolError
from tests.plugins.vcs.dirvcs_support import (
    DirWorld,
    empty_store,
    make_dir_world,
    set_head,
    stored_revision,
)
from tests.plugins.vcs.git_support import PUBLISH_BRANCH, write


@pytest.mark.parametrize("leg", ["decoy", "different", "ancestor"])
def test_directory_publication_destination_is_trusted(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    decoy = tmp_path / "decoy"
    empty_store(decoy)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    write(candidate, {"a.txt": "changed\n"})
    expected = None
    if leg == "decoy":
        (candidate / ".kora" / "repository.json").write_text(
            json.dumps({"path": str(decoy), "device": 0, "inode": 0})
        )
        (candidate / "redirect").symlink_to(decoy)
    else:
        expected = (
            world.base
            if leg == "ancestor"
            else stored_revision(
                world.remote, {"other": (store.REGULAR, b"other")}, world.base, "other"
            )
        )
        set_head(world.remote, PUBLISH_BRANCH, expected)

    prepared = world.prepare(checkout)
    outcome = world.publish(prepared)

    assert outcome.state == ("pushed" if leg == "decoy" else "conflict")
    assert world.remote_head() == (
        prepared.result.publication.commit if leg == "decoy" else expected
    )
    assert prepared.result.publication.base_commit == world.base
    assert list((decoy / "heads").iterdir()) == []


def replace_repository(world: DirWorld, tmp_path: Path) -> None:
    world.remote.rename(tmp_path / "old-remote")
    shutil.copytree(tmp_path / "old-remote", world.remote)


def test_repository_replaced_before_prepare_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"a.txt": "changed\n"})
    replace_repository(world, tmp_path)

    with pytest.raises(KoraProtocolError) as raised:
        world.prepare(checkout)

    assert raised.value.code == "vcs_destination_changed"


def test_repository_replaced_after_prepare_writes_no_head(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"a.txt": "changed\n"})
    prepared = world.prepare(checkout)
    replace_repository(world, tmp_path)

    with pytest.raises(ExecutorError) as raised:
        world.publish(prepared)

    assert raised.value.code == "destination_changed"
    assert world.remote_head() is None
    assert not (tmp_path / "old-remote" / "heads" / PUBLISH_BRANCH).exists()


def test_helper_refuses_a_mismatched_repository_pin(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"a.txt": "changed\n"})
    prepared = world.prepare(checkout)
    status = world.remote.stat()
    control = tmp_path / "wrong-pin.json"
    control.write_text(
        json.dumps(
            {
                "path": str(world.remote),
                "device": status.st_dev,
                "inode": status.st_ino + 1,
            }
        )
    )
    objects_before = sorted(path.name for path in (world.remote / "blobs").iterdir())

    with pytest.raises(store.Failed):
        store.publish(
            str(control),
            PUBLISH_BRANCH,
            world.base,
            prepared.result.publication.commit,
        )

    assert world.remote_head() is None
    assert (
        sorted(path.name for path in (world.remote / "blobs").iterdir())
        == objects_before
    )


def test_published_revision_checks_out_completely(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    write(candidate, {"a.txt": "changed\n", "nested/new.txt": "new\n"})
    write(candidate, {"keep/old.txt": None})
    (candidate / "tool.sh").write_text("#!/bin/sh\n")
    (candidate / "tool.sh").chmod(0o755)
    (candidate / "pointer").symlink_to("a.txt")
    shutil.copyfile(candidate / "run.sh", candidate / "moved.txt")
    prepared = world.prepare(checkout)

    outcome = world.publish(prepared)
    republished = world.candidate(world.checkout(world.remote_head()))

    assert outcome.state == "pushed"
    assert (republished / "a.txt").read_text() == "changed\n"
    assert (republished / "nested" / "new.txt").read_text() == "new\n"
    assert not (republished / "keep" / "old.txt").exists()
    assert (republished / "moved.txt").read_bytes() == b"#!/bin/sh\n"
    assert os.readlink(republished / "pointer") == "a.txt"
    assert (republished / "tool.sh").stat().st_mode & 0o111
    assert not (republished / "moved.txt").stat().st_mode & 0o111


def test_prepared_blob_changed_after_inspection_writes_no_head(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"a.txt": "changed\n"})
    prepared = world.prepare(checkout)
    location = store.digest_of(b"changed\n")
    blob = next(tmp_path.glob(f"bench-*/**/store/blobs/{location}"))
    blob.unlink()
    blob.write_bytes(b"swapped\n")

    outcome = world.publish(prepared)

    assert outcome.state == "unknown"
    assert world.remote_head() is None
    assert not (world.remote / "blobs" / location).exists()


def test_tampered_candidate_head_is_refused_at_export(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    write(candidate, {"a.txt": "changed\n"})
    (candidate / ".kora" / "dirvcs" / store.HEAD_NAME).write_text(
        "b" * 64, encoding="ascii"
    )

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout)

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
    assert raised.value.phase is Phase.EXPORT
    assert world.remote_head() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"branch": "kora/graph/run-1/{commit}/1"},
        {"commit_message": "kora: {input_location} #1"},
    ],
)
def test_placeholder_text_is_refused_before_planning(
    tmp_path: Path, kora_capture: Capture, changes: dict[str, str]
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    request = publish_request(world.remote_fixture(), world.checkout())
    forged = replace(request, **changes)

    with pytest.raises(KoraProtocolError) as raised:
        run(world.provider.publish_plan(forged, world.context()))

    assert raised.value.code == "vcs_request_invalid"


@pytest.mark.parametrize("leg", ["protected_deletion", "secret_path"])
def test_directory_uncommitted_changes_are_inspected(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    secret = world.capture.scope.admit("kora-directory-secret-filename")
    if leg == "protected_deletion":
        (candidate / "keep" / "old.txt").unlink()
    else:
        shutil.copyfile(
            candidate / "a.txt", candidate / "kora-directory-secret-filename"
        )

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout, secrets=(secret,), protected=("keep",))

    assert raised.value.failure_class is (
        ErrorClass.PROTECTED_PATH_MODIFIED
        if leg == "protected_deletion"
        else ErrorClass.CREDENTIAL_LEAK
    )
    assert raised.value.code == (
        "protected_path_modified" if leg == "protected_deletion" else "secret_leak"
    )
    assert world.remote_head() is None


def test_directory_export_rejects_special_files(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_dir_world(tmp_path, kora_capture)
    checkout = world.checkout()
    os.mkfifo(world.candidate(checkout) / "special")

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout)

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
    assert world.remote_head() is None
