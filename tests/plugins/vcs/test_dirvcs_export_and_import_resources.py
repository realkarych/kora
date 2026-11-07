import json
import os
import signal
from pathlib import Path

import pytest

from kora.plugins.vcs.fake import store


def test_export_walk_handles_deep_paths_with_bounded_descriptors(
    tmp_path: Path,
) -> None:
    root = os.open(tmp_path, store.DIRECTORY_FLAGS)
    current = os.dup(root)
    depth = 1100
    built = 0
    try:
        for _ in range(depth):
            os.mkdir("a", dir_fd=current)
            following = os.open("a", store.DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = following
            built += 1
        descriptor = os.open("file", store.NEW_FLAGS, 0o600, dir_fd=current)
        os.close(descriptor)
        baseline = len(os.listdir("/dev/fd"))

        found = list(store.working_files(root))

        assert found == ["a/" * depth + "file"]
        assert len(os.listdir("/dev/fd")) == baseline
    finally:
        os.unlink("file", dir_fd=current)
        for _ in range(built):
            parent = os.open("..", store.DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = parent
            os.rmdir("a", dir_fd=current)
        os.close(current)
        os.close(root)


def test_export_walk_bounds_empty_directory_entries(tmp_path: Path) -> None:
    for index in range(store.MAX_TREE_ENTRIES + 1):
        (tmp_path / str(index)).mkdir()
    root = os.open(tmp_path, store.DIRECTORY_FLAGS)
    baseline = len(os.listdir("/dev/fd"))

    try:
        with pytest.raises(store.Refused):
            list(store.working_files(root))
    finally:
        os.close(root)

    assert len(os.listdir("/dev/fd")) == baseline - 1


def test_export_walk_accepts_exactly_the_entry_bound_without_reserved_entries(
    tmp_path: Path,
) -> None:
    for index in range(store.MAX_TREE_ENTRIES - 1):
        (tmp_path / str(index)).mkdir()
    (tmp_path / "file").write_bytes(b"")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".kora").mkdir()
    (tmp_path / ".kora" / "reserved").write_bytes(b"")
    root = os.open(tmp_path, store.DIRECTORY_FLAGS)

    try:
        found = list(store.working_files(root))
    finally:
        os.close(root)

    assert found == ["file"]


@pytest.mark.parametrize("bundle", [False, True])
def test_import_refuses_a_forged_sealed_head(tmp_path: Path, bundle: bool) -> None:
    base = "a" * 64
    head = base if bundle else "b" * 64
    (tmp_path / "head.json").write_text(json.dumps({"head": head, "bundle": bundle}))

    with pytest.raises(store.Refused):
        store.import_base(str(tmp_path), base)


def test_helper_main_installs_its_termination_handler() -> None:
    previous = signal.getsignal(signal.SIGTERM)

    try:
        result = store.main(["unknown"])
        installed = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert result == store.REFUSED
    assert installed is store.terminate


def test_import_closes_chunk_when_scratch_creation_is_refused(tmp_path: Path) -> None:
    (tmp_path / "tree.0.bin").write_bytes(b"data")
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    source = os.open(tmp_path, store.DIRECTORY_FLAGS)
    target = os.open(blobs, store.DIRECTORY_FLAGS)
    entry = store.Entry("file", store.REGULAR, 4, store.digest_of(b"data"))
    record = store.SealedRecord(entry, 0, 0)
    baseline = len(os.listdir("/dev/fd"))
    blobs.chmod(0o500)

    try:
        with pytest.raises(PermissionError):
            store.import_sealed(source, record, target, set())
        assert len(os.listdir("/dev/fd")) == baseline
    finally:
        blobs.chmod(0o700)
        store.discard_all()
        os.close(target)
        os.close(source)

    assert list(blobs.iterdir()) == []
