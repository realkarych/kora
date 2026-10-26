import shutil
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.vcs import run
from kora.executor.entry import Executor
from kora.executor.failures import ExecutorError
from kora.executor.launcher import ProcessLauncher
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.plugins.vcs.git.remotes import RemoteRefused, remote_path
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import CheckoutRequest
from tests.plugins.vcs.git_support import (
    REPO,
    configuration,
    git,
    make_world,
    mapping,
    write,
)
from tests.plugins.vcs.launchers import RecordingLauncher


@pytest.mark.parametrize(
    "configured",
    [
        "relative/remote.git",
        "host:remote.git",
        "ssh://host/remote.git",
        "https://host/remote.git",
        "file://host/srv/remote.git",
        "file://user@/srv/remote.git",
        "file:///srv/remote.git?query",
        "file:///srv/remote.git#fragment",
        "file:///srv/%zzremote.git",
        "file:///srv/%ff.git",
        "file:///srv/../remote.git",
        "/srv/./remote.git",
        "ext::sh -c touch% /tmp/pwned",
    ],
)
def test_git_remote_paths_are_pinned(configured: str) -> None:
    with pytest.raises(RemoteRefused):
        remote_path(configured)


def test_escaped_filename_characters_survive_one_decode(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "a%20b.git").mkdir()
    (root / "c d.git").mkdir()

    escaped = remote_path(f"file://{root}/a%2520b.git")
    spaced = remote_path(f"file://{root}/c%20d.git")

    assert escaped == f"{root}/a%20b.git"
    assert spaced == f"{root}/c d.git"


def provider_for(tmp_path: Path, path: str) -> GitVcs:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    gone = tmp_path / "gone.git"
    if not gone.exists():
        git(home, "init", "-q", "--bare", str(gone))
    document = configuration(Path(path), gone)
    mapping(mapping(document["remotes"])[REPO])["path"] = path
    return GitVcs(GitConfiguration.model_validate(document))


def test_aliased_missing_and_non_bare_remotes_are_refused(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    bare = root / "bare.git"
    git(home, "init", "-q", "--bare", str(bare))
    (root / "alias.git").symlink_to(bare)
    git(home, "init", "-q", str(root / "worktree"))

    refused: list[str] = []
    for path in (
        str(root / "alias.git"),
        str(root / "absent.git"),
        str(root / "worktree"),
    ):
        with pytest.raises(KoraProtocolError) as raised:
            provider_for(root, path)
        refused.append(raised.value.code)

    assert refused == ["vcs_config_invalid"] * 3


def test_substitution_before_a_step_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    replacement = tmp_path / "replacement.git"
    git(world.home, "init", "-q", "--bare", str(replacement))
    shutil.rmtree(world.remote)
    replacement.rename(world.remote)
    request = CheckoutRequest(
        repo=REPO,
        rev=world.base,
        root=str(tmp_path / "candidate"),
        attempt_id="attempt",
        branch="work",
    )

    with pytest.raises(KoraProtocolError) as raised:
        run(world.provider.checkout_plan(request, context_for(kora_capture)))

    assert raised.value.code == "vcs_destination_changed"


def test_substitution_after_a_push_makes_the_outcome_unknown(
    tmp_path: Path, kora_capture: Capture
) -> None:
    launcher = RecordingLauncher()
    world = make_world(tmp_path, kora_capture, launcher=launcher)
    checkout = world.checkout()
    write(world.candidate(checkout), {"pinned.txt": "pinned\n"})
    prepared = world.prepare(checkout)
    moved = tmp_path / "moved.git"

    def substitute() -> None:
        world.remote.rename(moved)
        shutil.copytree(moved, world.remote, symlinks=True)

    launcher.after_push = substitute

    outcome = world.publish(prepared)

    assert outcome.state == "unknown"


def test_a_changed_pin_refuses_reconcile_and_survives_restart(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"restart.txt": "restart\n"})
    prepared = world.prepare(checkout)
    world.publish(prepared)
    copied = tmp_path / "copied.git"
    shutil.copytree(world.remote, copied, symlinks=True)
    shutil.rmtree(world.remote)
    copied.rename(world.remote)
    gone = tmp_path / "gone.git"
    git(world.home, "init", "-q", "--bare", str(gone))
    restarted = GitVcs(
        GitConfiguration.model_validate(configuration(world.remote, gone))
    )
    executor = Executor(
        restarted.tools, ProcessLauncher(), restarted, restarted, restarted
    )

    with pytest.raises(ExecutorError) as before_restart:
        run(world.executor.reconcile(prepared.result, world.context()))
    with pytest.raises(ExecutorError) as after_restart:
        run(executor.reconcile(prepared.result, world.context()))

    assert before_restart.value.code == "destination_changed"
    assert after_restart.value.code == "destination_changed"
