import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Checkout, publish_request, run
from kora.executor.failures import ExecutorError, Phase
from kora.ir.node import ErrorClass
from kora.plugins.vcs.fake import store
from kora.protocols.errors import KoraProtocolError
from tests.plugins.vcs.dirvcs_support import (
    SEED_FILES,
    DirWorld,
    empty_store,
    make_dir_world,
    set_head,
    stored_revision,
)
from tests.plugins.vcs.git_support import PUBLISH_BRANCH, write

DEEP_PATH: str = "/".join(["deep"] * 64 + ["file.txt"])


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


@pytest.mark.parametrize("committed", [False, True], ids=["uncommitted", "committed"])
def test_published_revision_checks_out_completely(
    tmp_path: Path, kora_capture: Capture, committed: bool
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
    if committed:
        world.commit(checkout, {DEEP_PATH: "deep\n"}, "committed change")
    prepared = world.prepare(checkout)

    outcome = world.publish(prepared)
    republished = world.candidate(world.checkout(world.remote_head()))

    assert outcome.state == "pushed"
    if committed:
        assert (republished / DEEP_PATH).read_text() == "deep\n"
    assert (republished / "a.txt").read_text() == "changed\n"
    assert (republished / "nested" / "new.txt").read_text() == "new\n"
    assert not (republished / "keep" / "old.txt").exists()
    assert (republished / "moved.txt").read_bytes() == b"#!/bin/sh\n"
    assert os.readlink(republished / "pointer") == "a.txt"
    assert (republished / "tool.sh").stat().st_mode & 0o111
    assert not (republished / "moved.txt").stat().st_mode & 0o111


def walked(objects: Path, head: str, stop: str | None) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    current: str | None = head
    while current != stop:
        assert current is not None
        document = cast(
            "dict[str, str]", json.loads((objects / "revisions" / current).read_bytes())
        )
        found.append({**document, "identifier": current})
        current = document["parent"]
    return found


def named_blobs(objects: Path, trees: Iterable[str]) -> set[str]:
    return {
        entry["sha256"]
        for tree in trees
        for entry in cast(
            "list[dict[str, str]]", json.loads((objects / "trees" / tree).read_bytes())
        )
    }


def expected_new_objects(objects: Path, head: str, base: str) -> set[str]:
    chain = walked(objects, head, base)
    ancestors = walked(objects, base, None)
    chain_blobs = named_blobs(objects, {item["tree"] for item in chain})
    held = named_blobs(objects, {item["tree"] for item in ancestors})
    return {item["identifier"] for item in chain} | (chain_blobs - held)


def prepared_work(checkout: Checkout) -> Path:
    publication = checkout.areas.publication
    assert publication is not None
    return next(publication.glob("prepared-*"))


def reachable_objects(
    world: DirWorld, work: Path, head: str, base: str
) -> frozenset[str]:
    async def reachable() -> frozenset[str]:
        async with world.provider.open(str(work)) as source:
            return await source.reachable(head, base, 30_000_000_000)

    return run(reachable())


def test_candidate_revisions_publish_completely(
    tmp_path: Path, kora_capture: Capture
) -> None:
    seeded = make_dir_world(tmp_path, kora_capture)
    without_alpha = {k: v for k, v in SEED_FILES.items() if k != "a.txt"}
    base = stored_revision(
        seeded.remote,
        {**without_alpha, "b.txt": (store.REGULAR, b"beta\n")},
        seeded.base,
        "parent",
    )
    world = replace(seeded, base=base)
    checkout = world.checkout()
    first = world.commit(checkout, {"a.txt": "alpha\n", "one.txt": "one\n"}, "first")
    second = world.commit(checkout, {"b.txt": None, "one.txt": None}, "second")
    third = world.commit(checkout, {"two.txt": "two\n"}, "third")

    prepared = world.prepare(checkout)
    work = prepared_work(checkout)
    expected = expected_new_objects(work / store.PREPARED_STORE, third, base)
    held_by_source = reachable_objects(world, work, third, base)
    index = [
        cast("dict[str, str]", json.loads(line))["content_location"]
        for line in (work / store.INDEX_NAME).read_bytes().splitlines()
    ]
    outcome = world.publish(prepared)
    republished = world.candidate(world.checkout(world.remote_head()))

    revisions = walked(world.remote, third, base)
    assert expected == {
        first,
        second,
        third,
        store.digest_of(b"one\n"),
        store.digest_of(b"two\n"),
    }
    assert held_by_source == expected
    assert set(index) == expected
    assert outcome.state == "pushed"
    assert prepared.result.publication.commit == third
    assert world.remote_head() == third
    assert [item["identifier"] for item in revisions] == [third, second, first]
    assert revisions[1]["tree"] == walked(world.remote, seeded.base, None)[0]["tree"]
    assert named_blobs(world.remote, {item["tree"] for item in revisions}) <= {
        path.name for path in (world.remote / "blobs").iterdir()
    }
    assert (world.remote / "blobs" / store.digest_of(b"one\n")).read_bytes() == b"one\n"
    assert (republished / "a.txt").read_text() == "alpha\n"
    assert (republished / "two.txt").read_text() == "two\n"
    assert not (republished / "b.txt").exists()
    assert not (republished / "one.txt").exists()


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
        {"commit_message": 'kora: a"b #1'},
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
