import os
from pathlib import Path

import pytest

from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    SealCapacityError,
    create_directory,
    remove_directory,
    snapshot_regular_tree,
)


@pytest.mark.parametrize("frozen", [False, True])
def test_anchored_tree_removal_refuses_substituted_name(
    tmp_path: Path, frozen: bool
) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    child = create_directory(parent, "a" * 32)
    original = tmp_path / ("a" * 32)
    moved = tmp_path / "retained"
    original.rename(moved)
    original.mkdir(mode=0o700)
    marker = original / "foreign"
    marker.write_text("retained")
    mode = 0o500 if frozen else 0o700
    original.chmod(mode)

    with pytest.raises(FilesystemError):
        remove_directory(parent, child.identity, restore_owner_write=frozen)

    assert marker.read_text() == "retained"
    assert original.stat().st_mode & 0o777 == mode
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


@pytest.mark.parametrize("frozen", [False, True])
def test_anchored_tree_removal_releases_owned_tree(
    tmp_path: Path, frozen: bool
) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    child = create_directory(parent, "c" * 32)
    nested = create_directory(child, "d" * 32)
    descriptor = os.open(
        "result", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=nested.descriptor
    )
    os.write(descriptor, b"owned")
    os.close(descriptor)
    if frozen:
        os.fchmod(nested.descriptor, 0o500)
    nested.close()
    child.close()

    remove_directory(parent, child.identity, restore_owner_write=frozen)

    assert not (tmp_path / ("c" * 32)).exists()
    parent.close()


def test_snapshot_stops_before_the_one_thousand_and_first_file(tmp_path: Path) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    source = create_directory(parent, "source")
    snapshot = create_directory(parent, "snapshot")
    for index in range(1001):
        descriptor = os.open(
            f"f{index:04d}",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=source.descriptor,
        )
        os.close(descriptor)

    with pytest.raises(SealCapacityError):
        snapshot_regular_tree(source, snapshot)

    assert _names(snapshot.descriptor) == []
    source.close()
    snapshot.close()
    parent.close()


def test_snapshot_stops_before_the_one_thousand_and_first_directory(
    tmp_path: Path,
) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    source = create_directory(parent, "source")
    snapshot = create_directory(parent, "snapshot")
    for index in range(1001):
        os.mkdir(f"d{index:04d}", 0o700, dir_fd=source.descriptor)

    with pytest.raises(SealCapacityError):
        snapshot_regular_tree(source, snapshot)

    assert _names(snapshot.descriptor) == []
    source.close()
    snapshot.close()
    parent.close()


def test_snapshot_accepts_one_thousand_directories(tmp_path: Path) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    source = create_directory(parent, "source")
    snapshot = create_directory(parent, "snapshot")
    for index in range(1000):
        os.mkdir(f"d{index:04d}", 0o700, dir_fd=source.descriptor)

    sealed = snapshot_regular_tree(source, snapshot)

    assert sealed.files == ()
    assert sealed.refused == ()
    assert _names(snapshot.descriptor) == [f"d{index:04d}" for index in range(1000)]
    source.close()
    snapshot.close()
    parent.close()


def test_unwritable_snapshot_refuses_listed_descendants(tmp_path: Path) -> None:
    parent = DirectoryAnchor.open_path(tmp_path)
    source = create_directory(parent, "source")
    snapshot = create_directory(parent, "snapshot")
    nested = create_directory(source, "nested")
    descriptor = os.open(
        "payload",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=nested.descriptor,
    )
    os.close(descriptor)
    os.fchmod(snapshot.descriptor, 0o500)

    sealed = snapshot_regular_tree(source, snapshot)

    assert sealed.files == ()
    assert sealed.refused == ("nested", "nested/payload")
    nested.close()
    source.close()
    snapshot.close()
    parent.close()


def _names(descriptor: int) -> list[str]:
    with os.scandir(descriptor) as entries:
        return sorted(entry.name for entry in entries)
