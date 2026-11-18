import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from kora.protocols.harness import Placement
from kora.runner.agent import refused
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    ReleaseState,
    open_private_directory,
    release_tree,
)

PLACEMENTS: Final[tuple[Placement, ...]] = (
    "harness_home",
    "harness_config",
    "harness_tmp",
    "harness_input",
)


class HarnessPlacements:
    def __init__(self, attempt: DirectoryAnchor, candidate: Path) -> None:
        attempt.verify()
        if not candidate.is_absolute() or candidate.name != "candidate":
            raise refused("launch_rejected")
        try:
            named = DirectoryAnchor.open_path(candidate.parent)
            try:
                if named.identity != attempt.identity:
                    raise refused("launch_rejected")
            finally:
                named.close()
        except (OSError, FilesystemError):
            raise refused("launch_rejected") from None
        self._attempt = attempt
        self._closed = False
        try:
            os.mkdir("harness", mode=0o700, dir_fd=attempt.descriptor)
        except OSError:
            raise refused("launch_rejected") from None
        try:
            directory = open_private_directory(attempt, "harness")
            try:
                for name in PLACEMENTS:
                    os.mkdir(name, mode=0o700, dir_fd=directory.descriptor)
                    child = open_private_directory(directory, name)
                    child.close()
            finally:
                directory.close()
        except (OSError, FilesystemError):
            self.close()
            raise refused("launch_rejected") from None
        self.paths: Mapping[Placement, Path] = MappingProxyType(
            {name: candidate.parent / "harness" / name for name in PLACEMENTS}
        )

    def close(self) -> None:
        if self._closed:
            return
        progress = release_tree(self._attempt, "harness")
        if progress.state is not ReleaseState.COMPLETE:
            raise FilesystemError("harness_cleanup_incomplete")
        self._closed = True
