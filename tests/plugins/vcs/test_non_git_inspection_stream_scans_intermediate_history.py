from collections.abc import Callable
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Checkout
from kora.executor.failures import ExecutorError
from kora.ir.node import ErrorClass
from tests.plugins.vcs.dirvcs_support import DirWorld, make_dir_world
from tests.plugins.vcs.git_support import World, make_world
from tests.plugins.vcs.launchers import RecordingLauncher

LEAKED_VALUE: str = "kora-intermediate-secret-77d2"


def in_file(world: World | DirWorld, checkout: Checkout) -> None:
    world.commit(checkout, {"notes.txt": f"{LEAKED_VALUE}\n"}, "intermediate")
    world.commit(checkout, {"notes.txt": "scrubbed\n"}, "scrubbed")


def in_message(world: World | DirWorld, checkout: Checkout) -> None:
    world.commit(checkout, {"notes.txt": "first\n"}, f"note {LEAKED_VALUE}")
    world.commit(checkout, {"notes.txt": "second\n"}, "scrubbed")


def in_symlink(world: World | DirWorld, checkout: Checkout) -> None:
    pointer = world.candidate(checkout) / "pointer"
    pointer.symlink_to(LEAKED_VALUE)
    world.commit(checkout, {}, "intermediate")
    pointer.unlink()
    world.commit(checkout, {}, "scrubbed")


def in_path(world: World | DirWorld, checkout: Checkout) -> None:
    world.commit(checkout, {LEAKED_VALUE: "content\n"}, "intermediate")
    world.commit(checkout, {LEAKED_VALUE: None}, "scrubbed")


def built_world(
    kind: str, root: Path, capture: Capture, launcher: RecordingLauncher
) -> World | DirWorld:
    if kind == "git":
        return make_world(root, capture, launcher=launcher)
    return make_dir_world(root, capture, launcher=launcher)


@pytest.mark.parametrize(
    "carrier",
    [
        pytest.param(in_file, id="file"),
        pytest.param(in_message, id="message"),
        pytest.param(in_symlink, id="symlink"),
        pytest.param(in_path, id="path"),
    ],
)
@pytest.mark.parametrize("kind", ["git", "directory"])
def test_non_git_inspection_stream_scans_intermediate_history(
    tmp_path: Path,
    kora_capture: Capture,
    kind: str,
    carrier: Callable[[World | DirWorld, Checkout], None],
) -> None:
    launcher = RecordingLauncher()
    world = built_world(kind, tmp_path, kora_capture, launcher)
    checkout = world.checkout()
    carrier(world, checkout)
    secret = world.capture.scope.admit(LEAKED_VALUE)

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout, secrets=(secret,))

    assert raised.value.failure_class is ErrorClass.CREDENTIAL_LEAK
    assert raised.value.code == "secret_leak"
    assert world.remote_head() is None
    assert launcher.specs
    for spec in launcher.specs:
        assert not any(LEAKED_VALUE in item for item in spec.argv)
        assert not any(LEAKED_VALUE in value for value in spec.env.values())
        assert spec.stdin is None or LEAKED_VALUE.encode() not in spec.stdin
