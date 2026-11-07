import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from kora.conformance.capture import Capture
from kora.executor.entry import Executor
from kora.executor.launcher import ProcessLauncher, StepLauncher
from kora.plugins.vcs.fake import DirVcs, store
from kora.plugins.vcs.local import LocalConfiguration
from tests.plugins.vcs.dirvcs_checkout_support import (
    configuration,
    empty_store,
    set_head,
    stored_revision,
)
from tests.plugins.vcs.git_support import PUBLISH_BRANCH, BaseWorld, ManualClock

__all__ = ["configuration", "empty_store", "stored_revision", "set_head"]

SEED_FILES: Final[dict[str, tuple[str, bytes]]] = {
    "a.txt": (store.REGULAR, b"alpha\n"),
    "keep/old.txt": (store.REGULAR, b"old\n"),
    "run.sh": (store.EXECUTABLE, b"#!/bin/sh\n"),
}


@dataclass(kw_only=True)
class DirWorld(BaseWorld[DirVcs]):
    missing_revision: ClassVar[str] = "1" * 64

    def remote_head(self, branch: str = PUBLISH_BRANCH) -> str | None:
        head = self.remote / "heads" / branch
        return head.read_text(encoding="ascii") if head.is_file() else None


def seeded_store(root: Path) -> tuple[Path, str]:
    remote = root / "remote.dirvcs"
    empty_store(remote)
    base = stored_revision(remote, SEED_FILES, None, "seed")
    set_head(remote, "main", base)
    return remote, base


def make_dir_world(
    root: Path,
    capture: Capture,
    launcher: StepLauncher | None = None,
    provider_type: Callable[[LocalConfiguration], DirVcs] = DirVcs,
) -> DirWorld:
    root = root.resolve()
    remote, base = seeded_store(root)
    gone = root / "gone.dirvcs"
    empty_store(gone)
    provider = provider_type(configuration(remote, gone))
    shutil.rmtree(gone)
    executor = Executor(
        provider.tools,
        launcher or ProcessLauncher(),
        provider,
        provider,
        provider,
    )
    return DirWorld(
        root=root,
        remote=remote,
        base=base,
        provider=provider,
        executor=executor,
        capture=capture,
        clock=ManualClock(),
    )
