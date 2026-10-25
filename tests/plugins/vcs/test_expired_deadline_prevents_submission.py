from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import (
    Kept,
    authorization,
    grant,
    pinned,
    publish_request,
    run,
)
from kora.executor.entry import PublishPermit
from kora.executor.failures import ExecutorError
from kora.executor.seal import ExportResult
from kora.ir.node import ErrorClass
from kora.protocols.deadlines import ExecutionDeadline
from tests.plugins.vcs.git_support import ManualClock, World, make_world, write
from tests.plugins.vcs.launchers import RecordingLauncher


def deadline_in(world: World, seconds: float) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=world.clock.now() + timedelta(seconds=seconds),
        scope_kind="attempt",
        scope_id="attempt",
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )


def sealing(world: World, launcher: RecordingLauncher) -> Callable[[], object]:
    checkout = world.checkout()
    plan = run(
        world.provider.publish_plan(
            publish_request(world.remote_fixture(), checkout), world.context()
        )
    )
    run(
        world.executor.export(
            plan, checkout.workspace(), checkout.areas, world.context()
        )
    )
    launcher.specs.clear()
    return lambda: run(
        world.executor.seal(
            ExportResult("kora.git.v1", ".kora/export"),
            checkout.workspace(),
            checkout.areas,
            world.context(deadline_in(world, 0)),
        )
    )


def preparation(world: World, launcher: RecordingLauncher) -> Callable[[], object]:
    checkout = world.checkout()
    remote = world.remote_fixture()
    context = world.context()
    plan = run(world.provider.publish_plan(publish_request(remote, checkout), context))
    workspace = checkout.workspace()
    exported = run(world.executor.export(plan, workspace, checkout.areas, context))
    sealed = run(world.executor.seal(exported, workspace, checkout.areas, context))
    launcher.specs.clear()
    return lambda: run(
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
            world.context(deadline_in(world, -1)),
        )
    )


def finalize(world: World, launcher: RecordingLauncher) -> Callable[[], object]:
    checkout = world.checkout()
    write(world.candidate(checkout), {"due.txt": "due\n"})
    prepared = world.prepare(checkout)
    result = prepared.result
    launcher.specs.clear()
    return lambda: run(
        world.executor.publish(
            result,
            prepared.plan,
            authorization(result),
            grant(),
            PublishPermit(result.publication.operation_id),
            world.context(deadline_in(world, 0)),
        )
    )


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (sealing, ErrorClass.TIMEOUT),
        (preparation, ErrorClass.TIMEOUT),
        (finalize, ErrorClass.CONFIG_ERROR),
    ],
)
def test_expired_deadline_prevents_submission(
    tmp_path: Path,
    kora_capture: Capture,
    phase: Callable[[World, RecordingLauncher], Callable[[], object]],
    expected: ErrorClass,
) -> None:
    launcher = RecordingLauncher()
    world = make_world(tmp_path, kora_capture, launcher=launcher, clock=ManualClock())
    attempt = phase(world, launcher)

    with pytest.raises(ExecutorError) as raised:
        attempt()

    assert raised.value.failure_class is expected
    assert launcher.specs == []
    assert world.remote_head() is None
