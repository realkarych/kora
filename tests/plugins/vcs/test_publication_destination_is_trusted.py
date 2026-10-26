from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.executor.digests import destination_digest, prepared_digest
from kora.ir.json import JsonObject
from tests.plugins.vcs.git_support import PUBLISH_BRANCH, git, make_world, write


def test_publication_destination_is_trusted(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    decoy = tmp_path / "decoy.git"
    git(world.home, "init", "-q", "--bare", str(decoy))
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    for key, value in (
        ("remote.origin.url", str(decoy)),
        ("remote.origin.pushurl", str(decoy)),
        (f"url.{decoy}.insteadOf", str(world.remote)),
        (f"url.{decoy}.pushInsteadOf", str(world.remote)),
        ("branch.conformance-work.remote", "origin"),
        ("include.path", str(tmp_path / "included")),
    ):
        git(world.home, "config", key, value, cwd=candidate)
    (tmp_path / "included").write_text(f'[remote "origin"]\n\turl = {decoy}\n')
    (candidate / "redirect").symlink_to(decoy)
    world.commit(checkout, {"a.txt": "changed\n"}, "candidate change")

    prepared = world.prepare(checkout)
    outcome = world.publish(prepared)

    publication = prepared.result.publication
    assert outcome.state == "pushed"
    assert world.remote_head() == publication.commit
    assert git(world.home, f"--git-dir={decoy}", "for-each-ref") == ""
    assert publication.base_commit == world.base
    assert publication.operation_id.startswith("op-")
    assert outcome.ref is not None
    assert outcome.ref.branch == PUBLISH_BRANCH
    assert (candidate / "redirect").is_symlink()


def test_publication_never_fast_forwards_an_ancestor(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    git(
        world.home,
        f"--git-dir={world.remote}",
        "update-ref",
        f"refs/heads/{PUBLISH_BRANCH}",
        world.base,
    )
    checkout = world.checkout()
    world.commit(checkout, {"a.txt": "descendant\n"}, "descendant")
    prepared = world.prepare(checkout)

    outcome = world.publish(prepared)

    assert outcome.state == "conflict"
    assert outcome.ref is None
    assert world.remote_head() == world.base


def test_destination_side_hooks_are_trusted_operator_configuration(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    marker = tmp_path / "destination-hook-ran"
    hook = world.remote / "hooks" / "post-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\n/usr/bin/touch {marker}\n")
    hook.chmod(0o755)
    checkout = world.checkout()
    write(world.candidate(checkout), {"hooked.txt": "hook\n"})
    prepared = world.prepare(checkout)

    outcome = world.publish(prepared)

    assert outcome.state == "pushed"
    assert marker.exists()


DESTINATION: tuple[str, str, str, str, str] = (
    "git",
    "/srv/remote.git",
    "repo",
    "kora/b",
    "c" * 40,
)


@pytest.mark.parametrize("changed", range(6))
def test_changing_any_destination_input_changes_the_digest(changed: int) -> None:
    transport: JsonObject = {"transport": "file", "binding_revision": 1}
    provider, remote, repo, branch, base = DESTINATION
    original = destination_digest(provider, remote, repo, branch, base, transport)
    altered = [
        f"{value}x" if index == changed else value
        for index, value in enumerate(DESTINATION)
    ]
    altered_transport: JsonObject = {
        **transport,
        "binding_revision": 2 if changed == 5 else 1,
    }

    digest = destination_digest(
        altered[0], altered[1], altered[2], altered[3], altered[4], altered_transport
    )

    assert digest != original


@pytest.mark.parametrize("changed", range(5))
def test_changing_any_prepared_input_changes_the_digest(changed: int) -> None:
    texts = ["a" * 40, "b" * 40, "s1", "p1"]
    raw = b"tree x\n"
    original = prepared_digest(texts[0], raw, texts[1], texts[2], texts[3])
    altered = [
        f"{text}y" if index == changed else text for index, text in enumerate(texts)
    ]
    altered_raw = raw + b"y" if changed == 4 else raw

    digest = prepared_digest(
        altered[0], altered_raw, altered[1], altered[2], altered[3]
    )

    assert digest != original
