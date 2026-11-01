from pathlib import Path
from typing import Final

from kora.executor.launcher import run_sync
from kora.plugins.vcs.git.environment import git_environment
from kora.plugins.vcs.local import RemoteRefused
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND

BARE_TIMEOUT_NS: Final[int] = 30 * NANOSECONDS_PER_SECOND


def check_bare(path: str, git: str, home: Path) -> None:
    root = Path(path)
    if not (
        (root / "HEAD").is_file()
        and (root / "objects").is_dir()
        and (root / "refs").is_dir()
    ):
        raise RemoteRefused("remote_not_bare")
    completed = run_sync(
        (git, f"--git-dir={path}", "rev-parse", "--is-bare-repository"),
        home,
        git_environment(git, str(home), ()),
        BARE_TIMEOUT_NS,
    )
    if completed.exit_code != 0 or completed.stdout.strip() != b"true":
        raise RemoteRefused("remote_not_bare")
