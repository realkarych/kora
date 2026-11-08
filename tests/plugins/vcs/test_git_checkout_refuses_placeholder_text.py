from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import WORK_BRANCH, run
from kora.executor.steps import PLACEHOLDER_NAMES
from kora.ir import VcsRef
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import CheckoutRequest
from tests.plugins.vcs.git_support import REPO, git, make_world

BRACE_TEXTS: Final[tuple[str, ...]] = (
    *(f"{{{name}}}" for name in PLACEHOLDER_NAMES),
    "{unknown}",
    "{open",
    "close}",
)
BRACE_BRANCH: Final[str] = "y/{control_root}"
BRACE_FREE_REF: Final[str] = "refs/heads/feature/plain"


@pytest.mark.parametrize("text", BRACE_TEXTS)
@pytest.mark.parametrize("field", ["branch", "rev"])
def test_git_checkout_refuses_placeholder_text(
    tmp_path: Path, kora_capture: Capture, field: str, text: str
) -> None:
    world = make_world(tmp_path, kora_capture)
    request = CheckoutRequest(
        repo=REPO,
        rev=f"refs/heads/x{text}" if field == "rev" else world.base,
        root=str(tmp_path / "candidate"),
        attempt_id="attempt",
        branch=f"x{text}" if field == "branch" else WORK_BRANCH,
    )

    with pytest.raises(KoraProtocolError) as raised:
        run(world.provider.checkout_plan(request, world.context()))

    assert raised.value.code == "vcs_request_invalid"


def test_git_head_sha_reads_brace_branch_literally(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    git(
        world.home,
        f"--git-dir={world.remote}",
        "update-ref",
        f"refs/heads/{BRACE_BRANCH}",
        world.base,
    )
    ref = VcsRef.model_validate(
        {
            "repo": REPO,
            "branch": BRACE_BRANCH,
            "commit": world.base,
            "base_commit": world.base,
            "changed_paths": [],
            "diff_stat": "",
            "pushed_by": world.provider.remotes[REPO].principal.model_dump(),
        }
    )

    assert run(world.provider.head_sha(ref, world.context())) == world.base


def test_git_checkout_accepts_brace_free_full_ref(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    git(
        world.home,
        f"--git-dir={world.remote}",
        "update-ref",
        BRACE_FREE_REF,
        world.base,
    )

    assert world.checkout(BRACE_FREE_REF).result.commit == world.base
