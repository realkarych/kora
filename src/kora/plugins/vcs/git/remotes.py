import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from kora.executor.launcher import run_sync
from kora.plugins.vcs.git.environment import git_environment
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND

FILE_SCHEME: Final[str] = "file://"
BAD_ESCAPE: Final[re.Pattern[str]] = re.compile(r"%(?![0-9A-Fa-f]{2})")
URL_SPECIAL: Final[frozenset[str]] = frozenset("?#")
BARE_TIMEOUT_NS: Final[int] = 30 * NANOSECONDS_PER_SECOND


class RemoteRefused(ValueError):
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


@dataclass(frozen=True)
class RemotePin:
    device: int
    inode: int

    @property
    def text(self) -> str:
        return f"{self.device}:{self.inode}"


def decoded_url(configured: str) -> str:
    rest = configured[len(FILE_SCHEME) :]
    if not rest.startswith("/") or URL_SPECIAL & set(rest):
        raise RemoteRefused("remote_url_refused")
    if BAD_ESCAPE.search(rest) is not None:
        raise RemoteRefused("remote_url_malformed")
    try:
        return unquote(rest, errors="strict")
    except UnicodeDecodeError:
        raise RemoteRefused("remote_url_malformed") from None


def remote_path(configured: str) -> str:
    if configured.startswith(FILE_SCHEME):
        path = decoded_url(configured)
    elif configured.startswith("/"):
        path = configured
    else:
        raise RemoteRefused("remote_form_refused")
    if "\x00" in path or not path.isprintable():
        raise RemoteRefused("remote_path_refused")
    parts = path.split("/")[1:]
    if any(part in (".", "..") for part in parts):
        raise RemoteRefused("remote_traversal_refused")
    normalized = os.path.normpath(path)
    if os.path.realpath(normalized) != normalized:
        raise RemoteRefused("remote_path_aliased")
    return normalized


def pin_of(path: str) -> RemotePin:
    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError:
        raise RemoteRefused("remote_missing") from None
    if not stat.S_ISDIR(status.st_mode):
        raise RemoteRefused("remote_not_bare")
    return RemotePin(status.st_dev, status.st_ino)


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
