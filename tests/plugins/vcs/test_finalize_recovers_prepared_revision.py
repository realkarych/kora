import shutil
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import (
    Kept,
    Prepared,
    authorization,
    grant,
    pinned,
    publish_request,
    run,
)
from kora.executor.entry import PublishPermit, RemoteState
from kora.executor.failures import ExecutorError
from kora.ir.node import ErrorClass
from kora.protocols.vcs import PublishRequest
from tests.plugins.vcs.git_support import (
    PUBLISH_BRANCH,
    World,
    git,
    make_world,
    write,
)


def held(world: World) -> Prepared:
    checkout = world.checkout()
    write(world.candidate(checkout), {"held.txt": "held\n"})
    return world.prepare(checkout)


def test_finalize_recovers_prepared_revision(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    prepared = held(world)

    absent = run(world.executor.reconcile(prepared.result, world.context()))
    pushed = world.publish(prepared)
    equal = run(world.executor.reconcile(prepared.result, world.context()))
    repeated = world.publish(prepared)

    assert absent is RemoteState.ABSENT
    assert pushed.state == "pushed"
    assert world.remote_head() == prepared.result.revision.commit
    assert equal is RemoteState.EQUAL
    assert repeated.ref == pushed.ref
    assert pushed.principal == prepared.result.pinned.binding.principal


def test_a_different_remote_value_is_a_conflict(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    prepared = held(world)
    git(
        world.home,
        f"--git-dir={world.remote}",
        "update-ref",
        f"refs/heads/{PUBLISH_BRANCH}",
        world.base,
    )

    state = run(world.executor.reconcile(prepared.result, world.context()))
    outcome = world.publish(prepared)

    assert state is RemoteState.DIFFERENT
    assert outcome.state == "conflict"


def test_an_unreachable_remote_stays_unknown(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    prepared = held(world)
    world.remote.chmod(0o000)

    try:
        outcome = world.publish(prepared)
        state = run(world.executor.reconcile(prepared.result, world.context()))
    finally:
        world.remote.chmod(0o755)

    assert outcome.state == "unknown"
    assert state is RemoteState.UNREACHABLE


def test_corrupt_prepared_state_fails_infrastructure(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    prepared = held(world)
    shutil.rmtree(prepared.result.revision.location)

    with pytest.raises(ExecutorError) as raised:
        world.publish(prepared)

    assert raised.value.failure_class is ErrorClass.INFRASTRUCTURE
    assert world.remote_head() is None


def test_a_changed_plan_is_a_workload_conflict(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    prepared = held(world)
    request = publish_request(world.remote_fixture(), prepared.checkout)
    changed = PublishRequest(
        repo=request.repo,
        root=request.root,
        attempt_id=request.attempt_id,
        branch=request.branch,
        commit_message="kora: agent #2",
    )
    other = run(world.provider.publish_plan(changed, world.context()))
    result = prepared.result

    with pytest.raises(ExecutorError) as publishing:
        run(
            world.executor.publish(
                result,
                other,
                authorization(result),
                grant(),
                PublishPermit(result.publication.operation_id),
                world.context(),
            )
        )
    with pytest.raises(ExecutorError) as preparing:
        run(
            world.executor.prepare_and_inspect(
                other,
                prepared.sealed,
                prepared.checkout.workspace(),
                changed,
                pinned(world.binding()),
                prepared.checkout.areas,
                (),
                (),
                Kept(),
                world.context(),
                result.revision,
            )
        )

    assert publishing.value.code == "workload_conflict"
    assert preparing.value.code == "workload_conflict"
    assert publishing.value.failure_class is ErrorClass.CONFIG_ERROR
    assert world.remote_head() is None
