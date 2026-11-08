from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import (
    Kept,
    authorization,
    grant,
    pinned,
    prepared_from,
    published,
    run,
    sealed_input,
)
from kora.executor.digests import destination_digest
from kora.executor.entry import PublishPermit
from kora.executor.failures import ExecutorError
from tests.plugins.vcs.git_support import (
    GONE_REPO,
    REPO,
    World,
    git,
    make_world,
    write,
)
from tests.plugins.vcs.launchers import RecordingLauncher

PLANNED_BRANCH: Final[str] = "kora/graph/run-1/other/1"
CHANGED_DESTINATIONS: Final[tuple[dict[str, str], ...]] = (
    {"branch": "kora/other/branch"},
    {"repo": GONE_REPO},
)


def remote_refs(world: World) -> str:
    return git(
        world.home,
        f"--git-dir={world.remote}",
        "for-each-ref",
        "--format=%(refname)",
    )


def test_publication_reports_the_planned_destination(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"planned.txt": "planned\n"})
    remote = replace(world.remote_fixture(), publish_branch=PLANNED_BRANCH)
    context = world.context()
    plan, sealed = run(
        sealed_input(world.provider, world.executor, remote, checkout, context)
    )
    prepared = run(
        prepared_from(world.executor, world.binding(), checkout, plan, sealed, context)
    )

    outcome = run(published(world.executor, prepared, context))

    publication = prepared.result.publication
    destination = world.provider.destination(REPO)
    assert plan.repo == publication.repo == REPO
    assert plan.branch == publication.branch == PLANNED_BRANCH
    assert outcome.ref is not None
    assert outcome.ref.branch == PLANNED_BRANCH
    assert publication.destination_digest == destination_digest(
        destination.provider,
        destination.remote,
        REPO,
        PLANNED_BRANCH,
        publication.base_commit,
        destination.transport,
    )
    assert f"refs/heads/{PLANNED_BRANCH}" in remote_refs(world).splitlines()


@pytest.mark.parametrize("update", CHANGED_DESTINATIONS)
def test_changed_plan_destination_is_a_workload_conflict(
    tmp_path: Path, kora_capture: Capture, update: dict[str, str]
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"held.txt": "held\n"})
    prepared = world.prepare(checkout)
    result = prepared.result
    changed = prepared.plan.model_copy(update=update)
    refs_before = remote_refs(world)

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.publish(
                result,
                changed,
                authorization(result),
                grant(),
                PublishPermit(result.publication.operation_id),
                world.context(),
            )
        )

    assert raised.value.code == "workload_conflict"
    assert remote_refs(world) == refs_before


@pytest.mark.parametrize(
    "update",
    [{"repo": GONE_REPO}, {"attempt_id": "another-attempt"}],
    ids=["another_repository", "another_attempt"],
)
def test_workspace_of_another_repository_is_a_workload_conflict(
    tmp_path: Path, kora_capture: Capture, update: dict[str, str]
) -> None:
    launcher = RecordingLauncher()
    world = make_world(tmp_path, kora_capture, launcher=launcher)
    checkout = world.checkout()
    write(world.candidate(checkout), {"held.txt": "held\n"})
    context = world.context()
    plan, sealed = run(
        sealed_input(
            world.provider, world.executor, world.remote_fixture(), checkout, context
        )
    )
    foreign = checkout.workspace().model_copy(update=update)
    sink = Kept()
    refs_before = remote_refs(world)
    launched_before = len(launcher.specs)

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_and_inspect(
                plan,
                sealed,
                foreign,
                pinned(world.binding()),
                checkout.areas,
                (),
                (),
                sink,
                context,
            )
        )

    assert raised.value.code == "workload_conflict"
    assert sink.revisions == []
    assert len(launcher.specs) == launched_before
    assert remote_refs(world) == refs_before
