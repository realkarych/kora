import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kora.executor.launcher import run_sync
from kora.executor.steps import ToolMissing, resolve_tools
from kora.plugins.vcs.git.environment import git_environment
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND

MINIMUM_GIT: Final[tuple[int, int, int]] = (2, 43, 0)
VERSION: Final[re.Pattern[bytes]] = re.compile(rb"git version (\d+)\.(\d+)\.(\d+)")
VERSION_TIMEOUT_NS: Final[int] = 30 * NANOSECONDS_PER_SECOND


class ToolRefused(ValueError):
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


@dataclass(frozen=True)
class GitTools:
    git: str
    python: str
    prlimit: str | None

    def mapping(self) -> dict[str, str]:
        tools = {"git": self.git, "python": self.python}
        if self.prlimit is not None:
            tools["prlimit"] = self.prlimit
        return tools

    def names(self) -> tuple[str, ...]:
        return tuple(self.mapping())


def needs_prlimit() -> bool:
    return sys.platform == "linux"


def git_version(git: str, home: Path) -> tuple[int, int, int]:
    completed = run_sync(
        (git, "--version"),
        home,
        git_environment(git, str(home), ()),
        VERSION_TIMEOUT_NS,
    )
    match = VERSION.match(completed.stdout)
    if completed.exit_code != 0 or match is None:
        raise ToolRefused("git_version_unreadable")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def resolve_git_tools(search_path: Sequence[str], home: Path) -> GitTools:
    names = ["git", *(["prlimit"] if needs_prlimit() else [])]
    try:
        resolved = resolve_tools(names, search_path)
    except ToolMissing:
        raise ToolRefused("tool_missing") from None
    version = git_version(resolved["git"], home)
    if version < MINIMUM_GIT:
        raise ToolRefused("git_too_old")
    return GitTools(
        git=resolved["git"],
        python=os.path.realpath(sys.executable),
        prlimit=resolved.get("prlimit"),
    )
