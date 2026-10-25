from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Checkout
from kora.executor.failures import ExecutorError
from kora.ir.node import ErrorClass
from kora.protocols.secret_value import SecretValue
from tests.plugins.vcs.git_support import World, git, make_world, write

LEAKED_VALUE: str = "kora-test-secret-4f1c9a"


def remote_log(world: World, commit: str) -> list[str]:
    output = git(
        world.home,
        f"--git-dir={world.remote}",
        "log",
        "--format=%H %P|%an",
        commit,
    )
    return output.splitlines()


def tree(world: World, commit: str) -> dict[str, tuple[str, str]]:
    listed = git(world.home, f"--git-dir={world.remote}", "ls-tree", "-r", commit)
    entries: dict[str, tuple[str, str]] = {}
    for line in listed.splitlines():
        header, path = line.split("\t", 1)
        mode, _, object_id = header.split(" ")
        entries[path] = (mode, object_id)
    return entries


def blob(world: World, object_id: str) -> bytes:
    return git(
        world.home, f"--git-dir={world.remote}", "cat-file", "blob", object_id
    ).encode()


def test_publication_preserves_candidate_commits(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    first = world.commit(checkout, {"a.txt": "first\n"}, "first", "First Author")
    (candidate / "binary.bin").write_bytes(bytes(range(256)) * 4)
    (candidate / "tool.sh").write_text("#!/bin/sh\n")
    (candidate / "tool.sh").chmod(0o755)
    (candidate / "pointer").symlink_to("a.txt")
    write(candidate, {".gitattributes": "* diff=marker\n", "a.txt": "alpha\n"})
    second = world.commit(checkout, {}, "second", "Second Author")

    prepared = world.prepare(checkout)
    outcome = world.publish(prepared)

    published = prepared.result.publication.commit
    log = remote_log(world, published)
    entries = tree(world, published)
    assert outcome.state == "pushed"
    assert published == second
    assert log[0] == f"{second} {first}|Second Author"
    assert log[1] == f"{first} {world.base}|First Author"
    assert entries["tool.sh"][0] == "100755"
    assert entries["pointer"][0] == "120000"
    assert blob(world, entries["pointer"][1]) == b"a.txt"
    assert entries["binary.bin"][0] == "100644"
    assert entries["a.txt"][1] == tree(world, world.base)["a.txt"][1]


def test_reverted_older_blob_imports_into_the_shallow_publisher(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    write(world.seed, {"a.txt": "second generation\n"})
    git(world.home, "commit", "-q", "-am", "second", cwd=world.seed)
    head = git(world.home, "rev-parse", "HEAD", cwd=world.seed)
    git(
        world.home,
        "push",
        "-q",
        str(world.remote),
        "HEAD:refs/heads/main",
        cwd=world.seed,
    )
    checkout = world.checkout(head)
    world.commit(checkout, {"a.txt": "alpha\n"}, "revert to the first generation")

    prepared = world.prepare(checkout)
    outcome = world.publish(prepared)

    assert outcome.state == "pushed"
    assert prepared.result.publication.changed_paths == ("a.txt",)


def test_worktree_gitlink_move_is_published_without_running_it(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    sub = candidate / "sub"
    git(world.home, "init", "-q", str(sub))
    write(sub, {"inner.txt": "one\n"})
    git(world.home, "add", "-A", cwd=sub)
    git(world.home, "commit", "-q", "-m", "inner one", cwd=sub)
    git(world.home, "add", "sub", cwd=candidate)
    git(world.home, "commit", "-q", "-m", "add gitlink", cwd=candidate)
    write(sub, {"inner.txt": "two\n"})
    git(world.home, "commit", "-q", "-am", "inner two", cwd=sub)
    moved = git(world.home, "rev-parse", "HEAD", cwd=sub)

    prepared = world.prepare(checkout)
    world.publish(prepared)

    entries = tree(world, prepared.result.publication.commit)
    assert entries["sub"] == ("160000", moved)


def secret_scope(world: World) -> tuple[SecretValue, ...]:
    return (world.capture.scope.admit(LEAKED_VALUE),)


def added_then_deleted(world: World, checkout: Checkout) -> None:
    world.commit(checkout, {"leak.txt": f"token={LEAKED_VALUE}\n"}, "add secret")
    world.commit(checkout, {"leak.txt": None}, "remove secret")


def unrelated_history_merge(world: World, checkout: Checkout) -> None:
    candidate = world.candidate(checkout)
    git(world.home, "checkout", "-q", "--orphan", "other", cwd=candidate)
    git(world.home, "rm", "-rfq", ".", cwd=candidate)
    write(candidate, {"orphan.txt": f"{LEAKED_VALUE}\n"})
    git(world.home, "add", "orphan.txt", cwd=candidate)
    git(world.home, "commit", "-q", "-m", "orphan", cwd=candidate)
    write(candidate, {"orphan.txt": "clean\n"})
    git(world.home, "commit", "-q", "-am", "orphan clean", cwd=candidate)
    git(world.home, "checkout", "-q", "conformance-work", cwd=candidate)
    git(
        world.home,
        "merge",
        "-q",
        "--allow-unrelated-histories",
        "-s",
        "ours",
        "-m",
        "merge orphan",
        "other",
        cwd=candidate,
    )


def evil_merge(world: World, checkout: Checkout) -> None:
    candidate = world.candidate(checkout)
    git(world.home, "checkout", "-q", "-b", "side", cwd=candidate)
    world.commit(checkout, {"side.txt": "side\n"}, "side")
    git(world.home, "checkout", "-q", "conformance-work", cwd=candidate)
    world.commit(checkout, {"main.txt": "main\n"}, "main")
    git(world.home, "merge", "-q", "--no-commit", "side", cwd=candidate)
    write(candidate, {"resolution.txt": f"{LEAKED_VALUE}\n"})
    git(world.home, "add", "resolution.txt", cwd=candidate)
    git(world.home, "commit", "-q", "-m", "evil merge", cwd=candidate)
    world.commit(checkout, {"resolution.txt": None}, "hide the resolution")


@pytest.mark.parametrize(
    "history", [added_then_deleted, unrelated_history_merge, evil_merge]
)
def test_secret_anywhere_in_new_history_is_a_leak(
    tmp_path: Path, kora_capture: Capture, history: object
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    assert callable(history)
    history(world, checkout)

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout, secrets=secret_scope(world))

    assert raised.value.failure_class is ErrorClass.CREDENTIAL_LEAK
    assert world.remote_head() is None


def test_required_lfs_is_refused(tmp_path: Path, kora_capture: Capture) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    world.commit(checkout, {".gitattributes": "*.bin filter=lfs diff=lfs\n"}, "lfs")

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout)

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
