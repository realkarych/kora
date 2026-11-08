from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import run
from kora.executor.steps import PLACEHOLDER_NAMES
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import PublishRequest
from tests.plugins.vcs.git_support import (
    COMMIT_MESSAGE,
    PUBLISH_BRANCH,
    REPO,
    World,
    git,
    make_world,
)

BRACE_TEXTS: Final[tuple[str, ...]] = (
    *(f"{{{name}}}" for name in PLACEHOLDER_NAMES),
    "{unknown}",
    "{open",
    "close}",
)
FORGED_VALUES: Final[tuple[tuple[str, str], ...]] = (
    *((f"kora/{text}/x", COMMIT_MESSAGE) for text in BRACE_TEXTS),
    *((PUBLISH_BRANCH, f"kora: {text} #1") for text in BRACE_TEXTS),
)


def remote_refs(world: World) -> str:
    return git(world.home, f"--git-dir={world.remote}", "for-each-ref")


@pytest.mark.parametrize(("branch", "message"), FORGED_VALUES)
def test_git_publication_refuses_placeholder_text(
    tmp_path: Path, kora_capture: Capture, branch: str, message: str
) -> None:
    world = make_world(tmp_path, kora_capture)
    request = PublishRequest(
        repo=REPO,
        root=str(tmp_path / "candidate"),
        attempt_id="attempt",
        branch=branch,
        commit_message=message,
    )
    refs_before = remote_refs(world)

    with pytest.raises(KoraProtocolError) as raised:
        run(world.provider.publish_plan(request, world.context()))

    assert raised.value.code == "vcs_request_invalid"
    assert remote_refs(world) == refs_before
