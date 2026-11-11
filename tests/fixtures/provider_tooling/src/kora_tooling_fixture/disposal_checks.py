import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.plugins.storage.local import LocalStorageConfiguration
from kora_tooling_fixture.configured_storage import ConfiguredStorage, StoreReceivers
from kora_tooling_fixture.owned_directory import (
    Identity,
    directory_identity,
    dispose_owned_directory,
    dispose_partial_directory,
)


async def unsettled_auxiliary(root: Path) -> None:
    receivers = StoreReceivers()
    configuration = LocalStorageConfiguration.model_validate(
        {"root": str(root), "limits": {"version": 1}}
    )
    primary = ConfiguredStorage(configuration, receivers)
    auxiliary = ConfiguredStorage(configuration, receivers)
    await primary.aclose()

    with pytest.raises(AssertionError):
        receivers.dispose(root)

    assert root.is_dir()
    assert not auxiliary.closed_confirmed
    await auxiliary.aclose()
    receivers.dispose(root)
    assert not root.exists()


def filesystem_refusal(root: Path, mode: str) -> None:
    root.mkdir(mode=0o700)
    (root / "retained").write_bytes(b"owned-content")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identity = directory_identity(descriptor)
    os.close(descriptor)
    external = root.with_name("outside")
    if mode == "outside_link":
        os.link(root / "retained", external)
    else:
        root.rename(external)
        root.mkdir(mode=0o700)
        (root / "borrowed").write_bytes(b"borrowed-content")

    with pytest.raises(AssertionError):
        dispose_owned_directory(root, identity)

    assert root.is_dir()
    if mode == "outside_link":
        assert external.read_bytes() == b"owned-content"
        assert (root / "retained").read_bytes() == b"owned-content"
    else:
        assert (root / "borrowed").read_bytes() == b"borrowed-content"
        assert (external / "retained").read_bytes() == b"owned-content"


def created_identity(root: Path) -> Identity:
    root.mkdir(mode=0o700)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return directory_identity(descriptor)
    finally:
        os.close(descriptor)


def outside_content(root: Path) -> Path:
    outside = root.with_name("outside")
    outside.mkdir(mode=0o700)
    (outside / "kept").write_bytes(b"outside-content")
    return outside


def partial_disposal_spares_link_targets(root: Path) -> None:
    identity = created_identity(root)
    outside = outside_content(root)
    (root / "directory-link").symlink_to(outside)
    (root / "file-link").symlink_to(outside / "kept")
    os.link(outside / "kept", root / "hard-link")

    dispose_partial_directory(root, identity)

    assert not os.path.lexists(root)
    assert [path.name for path in outside.iterdir()] == ["kept"]
    assert (outside / "kept").read_bytes() == b"outside-content"
    assert (outside / "kept").stat().st_nlink == 1


def partial_disposal_refuses_substitution(root: Path) -> None:
    identity = created_identity(root)
    root.rename(root.with_name("moved"))
    root.mkdir(mode=0o700)
    (root / "borrowed").write_bytes(b"borrowed-content")

    with pytest.raises(AssertionError):
        dispose_partial_directory(root, identity)

    assert (root / "borrowed").read_bytes() == b"borrowed-content"
    assert root.with_name("moved").is_dir()


def main(mode: str) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve() / "store"
        if mode == "auxiliary":
            asyncio.run(unsettled_auxiliary(root))
        elif mode == "partial_links":
            partial_disposal_spares_link_targets(root)
        elif mode == "partial_substitution":
            partial_disposal_refuses_substitution(root)
        else:
            filesystem_refusal(root, mode)


if __name__ == "__main__":
    main(sys.argv[1])
