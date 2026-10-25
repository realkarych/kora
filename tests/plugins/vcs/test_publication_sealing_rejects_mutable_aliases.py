import json
import os
import threading
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Checkout, publish_request, run
from kora.executor.failures import ExecutorError
from kora.executor.seal import (
    ExportResult,
    ManifestEntry,
    SealBounds,
    SealedWriter,
    seal_entry,
)
from kora.ir.node import ErrorClass
from kora.protocols.vcs import PublicationInput
from tests.plugins.vcs.git_support import World, make_world, write


def exported(world: World, checkout: Checkout) -> Path:
    context = world.context()
    plan = run(
        world.provider.publish_plan(
            publish_request(world.remote_fixture(), checkout), context
        )
    )
    run(world.executor.export(plan, checkout.workspace(), checkout.areas, context))
    return world.candidate(checkout) / ".kora" / "export"


def sealed(world: World, checkout: Checkout) -> PublicationInput:
    context = world.context()
    return run(
        world.executor.seal(
            ExportResult("kora.git.v1", ".kora/export"),
            checkout.workspace(),
            checkout.areas,
            context,
        )
    )


def refused_class(world: World, checkout: Checkout) -> ErrorClass:
    with pytest.raises(ExecutorError) as raised:
        sealed(world, checkout)
    return raised.value.failure_class


def test_publication_sealing_rejects_mutable_aliases(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "old.txt").write_text("outside secret\n")
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    exported(world, checkout)
    (candidate / "keep" / "old.txt").unlink()
    (candidate / "keep").rmdir()
    (candidate / "keep").symlink_to(outside)

    failure = refused_class(world, checkout)

    assert failure is ErrorClass.ARTIFACT_INVALID


def test_escaping_symlink_is_sealed_as_data(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    (candidate / "escape").symlink_to("/etc/passwd")
    target = candidate / "hardlinked.txt"
    os.link(candidate / "a.txt", target)

    prepared = world.prepare(checkout)
    world.publish(prepared)

    assert {"escape", "hardlinked.txt"} <= set(
        prepared.result.publication.changed_paths
    )


def test_special_file_is_refused(tmp_path: Path, kora_capture: Capture) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    os.mkfifo(world.candidate(checkout) / "pipe")
    export = exported(world, checkout)
    with (export / "manifest.jsonl").open("a") as manifest:
        manifest.write(json.dumps({"path": "pipe", "gitlink": None}) + "\n")

    failure = refused_class(world, checkout)

    assert failure is ErrorClass.ARTIFACT_INVALID


@pytest.mark.parametrize(
    "paths",
    [
        ["/absolute"],
        ["../escape"],
        ["a/./b"],
        ["a//b"],
        ["control\x01char"],
        [".git/config"],
        [".GIT/config"],
        [".git./config"],
        [".git /config"],
        ["git~1/config"],
        [".g‌it/config"],
        [".git::$INDEX_ALLOCATION/config"],
        [".kora/plan"],
        ["sub/.KORA"],
        ["a.txt", "a.txt"],
        ["a.txt", "A.TXT"],
    ],
)
def test_manifest_path_refusals(
    tmp_path: Path, kora_capture: Capture, paths: list[str]
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    export = exported(world, checkout)
    lines = [json.dumps({"path": path, "gitlink": None}) for path in paths]
    (export / "manifest.jsonl").write_text("".join(line + "\n" for line in lines))

    failure = refused_class(world, checkout)

    assert failure is ErrorClass.ARTIFACT_INVALID


def test_file_on_another_device_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    output = tmp_path / "output"
    root.mkdir()
    output.mkdir()
    write(root, {"file.txt": "data\n"})
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    output_descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    writer = SealedWriter(output_descriptor, SealBounds(), threading.Event())
    device = os.fstat(root_descriptor).st_dev

    try:
        with pytest.raises(ExecutorError) as raised:
            seal_entry(
                writer, root_descriptor, ManifestEntry("file.txt", None), device + 1
            )
    finally:
        os.close(root_descriptor)
        os.close(output_descriptor)

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID


def test_candidate_changes_after_sealing_do_not_change_the_revision(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    write(candidate, {"a.txt": "sealed\n"})
    exported(world, checkout)
    sealed_input = sealed(world, checkout)
    write(candidate, {"a.txt": "rewritten after sealing\n", "late.txt": "late\n"})

    verified = run(world.executor.verify_sealed(sealed_input, checkout.areas))

    assert verified.digest == sealed_input.digest
