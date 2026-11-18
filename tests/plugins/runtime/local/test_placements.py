import stat
from pathlib import Path
from typing import Literal

import pytest

from kora.plugins.runtime.local.placements import HarnessPlacements
from kora.protocols.errors import KoraProtocolError
from kora.runner.filesystem import DirectoryAnchor


def test_harness_placements_are_fresh_and_removed_without_following_links(
    tmp_path: Path,
) -> None:
    attempt = tmp_path.resolve() / "attempt"
    attempt.mkdir(mode=0o700)
    roots = tuple(attempt / name for name in ("candidate", "control", "publication"))
    for root in roots:
        root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    preserved = outside / "keep"
    preserved.write_text("unchanged")
    anchor = DirectoryAnchor.open_path(attempt)

    try:
        placements = HarnessPlacements(anchor, roots[0])
        paths = tuple(placements.paths.values())
        modes = tuple(stat.S_IMODE(path.stat().st_mode) for path in paths)
        (paths[0] / "link").symlink_to(outside, target_is_directory=True)
        placements.close()
        placements.close()
    finally:
        anchor.close()

    assert len(set(paths)) == 4
    assert modes == (0o700,) * 4
    assert all(path.parent == attempt / "harness" for path in paths)
    assert all(not path.is_relative_to(root) for path in paths for root in roots)
    assert not (attempt / "harness").exists()
    assert preserved.read_text() == "unchanged"


@pytest.mark.parametrize("kind", ["directory", "symlink", "file"])
def test_harness_placements_refuse_existing_paths(
    tmp_path: Path, kind: Literal["directory", "symlink", "file"]
) -> None:
    attempt = tmp_path.resolve() / "attempt"
    attempt.mkdir(mode=0o700)
    existing = attempt / "harness"
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "directory":
        existing.mkdir(mode=0o700)
    elif kind == "symlink":
        existing.symlink_to(outside, target_is_directory=True)
    else:
        existing.write_text("keep")
    before = existing.lstat()
    anchor = DirectoryAnchor.open_path(attempt)

    try:
        with pytest.raises(KoraProtocolError) as caught:
            HarnessPlacements(anchor, attempt / "candidate")
    finally:
        anchor.close()

    assert caught.value.code == "launch_rejected"
    assert existing.lstat() == before
    assert tuple(outside.iterdir()) == ()


def test_harness_placements_refuse_unbound_attempt_path(tmp_path: Path) -> None:
    attempt = tmp_path.resolve() / "attempt"
    unrelated = tmp_path.resolve() / "unrelated"
    attempt.mkdir(mode=0o700)
    unrelated.mkdir(mode=0o700)
    anchor = DirectoryAnchor.open_path(attempt)

    try:
        with pytest.raises(KoraProtocolError) as caught:
            HarnessPlacements(anchor, unrelated / "candidate")
    finally:
        anchor.close()

    assert caught.value.code == "launch_rejected"
    assert tuple(attempt.iterdir()) == ()
    assert tuple(unrelated.iterdir()) == ()
