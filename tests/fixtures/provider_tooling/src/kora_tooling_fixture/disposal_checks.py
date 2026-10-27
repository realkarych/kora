import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.plugins.storage.local import LocalStorageConfiguration
from kora_tooling_fixture.configured_storage import ConfiguredStorage, StoreReceivers
from kora_tooling_fixture.owned_directory import (
    directory_identity,
    dispose_owned_directory,
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


def main(mode: str) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve() / "store"
        if mode == "auxiliary":
            asyncio.run(unsettled_auxiliary(root))
        else:
            filesystem_refusal(root, mode)


if __name__ == "__main__":
    main(sys.argv[1])
