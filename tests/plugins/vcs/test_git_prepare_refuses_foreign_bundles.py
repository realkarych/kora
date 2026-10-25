import json
from collections.abc import Callable
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import (
    Checkout,
    Kept,
    pinned,
    publish_request,
    run,
)
from kora.executor.failures import ExecutorError
from kora.ir.node import ErrorClass
from tests.plugins.vcs.git_support import World, git, make_world

Forge = Callable[[World, Path, Path], str]


def commit_on(world: World, candidate: Path, parent: str, name: str) -> str:
    tree = git(world.home, "rev-parse", f"{parent}^{{tree}}", cwd=candidate)
    return git(world.home, "commit-tree", tree, "-p", parent, "-m", name, cwd=candidate)


def full_history(world: World, candidate: Path, bundle: Path) -> str:
    head = commit_on(world, candidate, world.base, "full")
    git(world.home, "update-ref", "refs/heads/full", head, cwd=candidate)
    git(world.home, "bundle", "create", "-q", str(bundle), "full", cwd=candidate)
    return head


def foreign_prerequisite(world: World, candidate: Path, bundle: Path) -> str:
    middle = commit_on(world, candidate, world.base, "middle")
    head = commit_on(world, candidate, middle, "head")
    git(world.home, "update-ref", "refs/heads/forged", head, cwd=candidate)
    git(
        world.home,
        "bundle",
        "create",
        "-q",
        str(bundle),
        "forged",
        f"^{middle}",
        cwd=candidate,
    )
    return head


def extra_heads(world: World, candidate: Path, bundle: Path) -> str:
    first = commit_on(world, candidate, world.base, "first")
    second = commit_on(world, candidate, world.base, "second")
    git(world.home, "update-ref", "refs/heads/one", first, cwd=candidate)
    git(world.home, "update-ref", "refs/heads/two", second, cwd=candidate)
    git(
        world.home,
        "bundle",
        "create",
        "-q",
        str(bundle),
        "one",
        "two",
        f"^{world.base}",
        cwd=candidate,
    )
    return first


def different_head(world: World, candidate: Path, bundle: Path) -> str:
    head = commit_on(world, candidate, world.base, "bundled")
    git(world.home, "update-ref", "refs/heads/bundled", head, cwd=candidate)
    git(
        world.home,
        "bundle",
        "create",
        "-q",
        str(bundle),
        "bundled",
        f"^{world.base}",
        cwd=candidate,
    )
    return commit_on(world, candidate, world.base, "claimed")


def unrelated_head(world: World, candidate: Path, bundle: Path) -> str:
    tree = git(world.home, "rev-parse", f"{world.base}^{{tree}}", cwd=candidate)
    orphan = git(world.home, "commit-tree", tree, "-m", "orphan", cwd=candidate)
    git(world.home, "update-ref", "refs/heads/orphan", orphan, cwd=candidate)
    git(
        world.home,
        "bundle",
        "create",
        "-q",
        str(bundle),
        "orphan",
        f"^{world.base}",
        cwd=candidate,
    )
    return orphan


def malformed_commit(world: World, candidate: Path, bundle: Path) -> str:
    tree = git(world.home, "rev-parse", f"{world.base}^{{tree}}", cwd=candidate)
    raw = f"tree {tree}\nparent {world.base}\nauthor nobody\ncommitter nobody\n\nbad\n"
    scratch = candidate.parent / "malformed"
    scratch.write_text(raw)
    head = git(
        world.home,
        "hash-object",
        "-t",
        "commit",
        "--literally",
        "-w",
        str(scratch),
        cwd=candidate,
    )
    git(world.home, "update-ref", "refs/heads/malformed", head, cwd=candidate)
    git(
        world.home,
        "bundle",
        "create",
        "-q",
        str(bundle),
        "malformed",
        f"^{world.base}",
        cwd=candidate,
    )
    return head


def prepared_class(world: World, checkout: Checkout, forge: Forge) -> ErrorClass:
    context = world.context()
    remote = world.remote_fixture()
    plan = run(world.provider.publish_plan(publish_request(remote, checkout), context))
    workspace = checkout.workspace()
    exported = run(world.executor.export(plan, workspace, checkout.areas, context))
    candidate = world.candidate(checkout)
    export = candidate / ".kora" / "export"
    head = forge(world, candidate, export / "head.bundle")
    (export / "head.json").write_text(json.dumps({"head": head, "bundle": True}))
    sealed = run(world.executor.seal(exported, workspace, checkout.areas, context))
    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_and_inspect(
                plan,
                sealed,
                workspace,
                publish_request(remote, checkout),
                pinned(world.binding()),
                checkout.areas,
                (),
                (),
                Kept(),
                context,
            )
        )
    return raised.value.failure_class


@pytest.mark.parametrize(
    "forge",
    [
        full_history,
        foreign_prerequisite,
        extra_heads,
        different_head,
        unrelated_head,
        malformed_commit,
    ],
)
def test_git_prepare_refuses_foreign_bundles(
    tmp_path: Path, kora_capture: Capture, forge: Forge
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()

    failure = prepared_class(world, checkout, forge)

    assert failure is ErrorClass.ARTIFACT_INVALID
    assert world.remote_head() is None
