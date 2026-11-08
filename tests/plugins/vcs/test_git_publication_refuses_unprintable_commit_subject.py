from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import prepared_from, published, run, sealed_input
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import PublishRequest
from tests.plugins.vcs.git_support import PUBLISH_BRANCH, REPO, git, make_world, write

UNPRINTABLE_MESSAGES: Final[tuple[str, ...]] = (
    "kora: \x1b[2J\x07 #1",
    "kora: ‮evil‬ #1",
    "kora: a​b #1",
    "kora: \x7f #1",
    "kora: a\xadb #1",
    "kora: аgent #1",
    f"kora: {'a' * 1025} #1",
)
NODE_PATH_MESSAGE: Final[str] = "kora: candidates[2]@3.review #1"


@pytest.mark.parametrize("message", UNPRINTABLE_MESSAGES)
def test_git_publication_refuses_unprintable_commit_subject(
    tmp_path: Path, kora_capture: Capture, message: str
) -> None:
    world = make_world(tmp_path, kora_capture)
    request = PublishRequest(
        repo=REPO,
        root=str(tmp_path / "candidate"),
        attempt_id="attempt",
        branch=PUBLISH_BRANCH,
        commit_message=message,
    )

    with pytest.raises(KoraProtocolError) as raised:
        run(world.provider.publish_plan(request, world.context()))

    assert raised.value.code == "vcs_request_invalid"


def test_git_publication_keeps_node_path_subject(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"change.txt": "change\n"})
    remote = replace(world.remote_fixture(), commit_message=NODE_PATH_MESSAGE)
    context = world.context()
    plan, sealed = run(
        sealed_input(world.provider, world.executor, remote, checkout, context)
    )
    prepared = run(
        prepared_from(world.executor, world.binding(), checkout, plan, sealed, context)
    )

    outcome = run(published(world.executor, prepared, context))

    assert outcome.state == "pushed"
    subject = git(
        world.home,
        f"--git-dir={world.remote}",
        "log",
        "-1",
        "--format=%s",
        f"refs/heads/{PUBLISH_BRANCH}",
    )
    assert subject.strip() == NODE_PATH_MESSAGE
