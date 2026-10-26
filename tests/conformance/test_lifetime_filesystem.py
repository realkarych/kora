import os
from pathlib import Path

import pytest

from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    create_directory,
    remove_directory,
)


def test_anchored_tree_removal_refuses_substituted_name(tmp_path: Path) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    child = create_directory(parent, "a" * 32)
    original = tmp_path / ("a" * 32)
    moved = tmp_path / "retained"
    original.rename(moved)
    original.mkdir(mode=0o700)
    marker = original / "foreign"
    marker.write_text("retained")

    with pytest.raises(FilesystemError):
        remove_directory(parent, child.identity)

    assert marker.read_text() == "retained"
    assert moved.is_dir()
    child.close()
    parent.close()


def test_anchored_tree_removal_refuses_symlink_descendant(tmp_path: Path) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    child = create_directory(parent, "b" * 32)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    marker = outside / "retained"
    marker.write_text("foreign")
    os.symlink(outside, "link", dir_fd=child.descriptor)

    with pytest.raises(FilesystemError):
        remove_directory(parent, child.identity)

    assert marker.read_text() == "foreign"
    assert (tmp_path / ("b" * 32) / "link").is_symlink()
    child.close()
    parent.close()


def test_anchored_tree_removal_releases_owned_tree(tmp_path: Path) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    child = create_directory(parent, "c" * 32)
    nested = create_directory(child, "d" * 32)
    descriptor = os.open(
        "result", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=nested.descriptor
    )
    os.write(descriptor, b"owned")
    os.close(descriptor)
    nested.close()
    child.close()

    remove_directory(parent, child.identity)

    assert not (tmp_path / ("c" * 32)).exists()
    parent.close()
