from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.vcs import authorization, grant, run
from kora.executor.entry import PublishPermit, RemoteState
from tests.plugins.vcs.git_support import make_world, write
from tests.plugins.vcs.launchers import RecordingLauncher


def test_publish_reconciles_after_response_loss(
    tmp_path: Path, kora_capture: Capture
) -> None:
    launcher = RecordingLauncher(drop_push_response=True)
    world = make_world(tmp_path, kora_capture, launcher=launcher)
    checkout = world.checkout()
    write(world.candidate(checkout), {"lost.txt": "lost response\n"})
    prepared = world.prepare(checkout)
    result = prepared.result

    lost = world.publish(prepared)
    state = run(world.executor.reconcile(result, world.context()))
    without_permit = run(
        world.executor.publish(
            result,
            prepared.plan,
            authorization(result),
            grant(),
            None,
            world.context(),
            lost,
        )
    )
    other_operation = run(
        world.executor.publish(
            result,
            prepared.plan,
            authorization(result),
            grant(),
            PublishPermit("another-operation"),
            world.context(),
        )
    )

    assert lost.state == "unknown"
    assert world.remote_head() == result.revision.commit
    assert state is RemoteState.EQUAL
    assert without_permit == lost
    assert other_operation.state == "unknown"
    assert launcher.pushes() == 1
