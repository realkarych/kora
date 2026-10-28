import errno
import os
from pathlib import Path

import pytest

from kora.plugins.storage.files import RootAnchor, RootManifest
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageIdentity, StorageLimits


def descriptors_inside(store: Path) -> frozenset[tuple[int, int, int]]:
    objects = {
        (info.st_dev, info.st_ino)
        for info in (store.lstat(), *(path.lstat() for path in store.rglob("*")))
    }
    owned: set[tuple[int, int, int]] = set()
    for name in os.listdir("/dev/fd"):
        try:
            info = os.fstat(int(name))
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue
        if (info.st_dev, info.st_ino) in objects:
            owned.add((int(name), info.st_dev, info.st_ino))
    return frozenset(owned)


def test_local_storage_identity_is_physical(tmp_path: Path) -> None:
    configured = tmp_path / "store"
    alias = tmp_path / "alias"
    alias.symlink_to(configured, target_is_directory=True)
    limits = StorageLimits(version=1)

    with RootAnchor.open(configured, limits) as first:
        with RootAnchor.open(alias, limits) as second:
            identity = second.identity
        with RootAnchor.open(configured, limits) as restarted:
            recovered = restarted.identity

    assert identity == first.identity == recovered
    assert configured.stat().st_mode & 0o777 == 0o700
    assert (configured / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_local_storage_refuses_copied_identity_and_overlapping_domains(
    tmp_path: Path,
) -> None:
    limits = StorageLimits(version=1)
    configured = tmp_path / "store"
    copied = tmp_path / "copy"
    with RootAnchor.open(configured, limits):
        manifest = (configured / "manifest.json").read_bytes()
    copied.mkdir(mode=0o700)
    (copied / "manifest.json").write_bytes(manifest)
    (copied / "manifest.json").chmod(0o600)

    with pytest.raises(KoraProtocolError) as copied_identity:
        RootAnchor.open(copied, limits)
    with pytest.raises(KoraProtocolError) as overlapping_domain:
        RootAnchor.open(configured / "nested", limits)

    assert copied_identity.value.code == "storage_identity"
    assert overlapping_domain.value.code == "storage_identity"


def test_local_storage_root_configuration_is_immutable(tmp_path: Path) -> None:
    configured = tmp_path / "store"
    with RootAnchor.open(configured, StorageLimits(version=1)):
        pass

    with pytest.raises(KoraProtocolError) as changed_limits:
        RootAnchor.open(configured, StorageLimits(version=1, stage_slots=9))

    assert changed_limits.value.code == "storage_identity"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("identity", "storage_identity"),
        ("malformed", "storage_identity"),
        ("oversized", "storage_identity"),
        ("mode", "storage_path"),
    ],
    ids=["identity", "malformed", "oversized", "mode"],
)
def test_refreshed_manifest_refuses_changed_input_after_a_validated_hit(
    tmp_path: Path, change: str, code: str
) -> None:
    configured = tmp_path / "store"
    with RootAnchor.open(configured, StorageLimits(version=1)) as root:
        root.refresh_manifest()
        root.refresh_manifest()
        path = configured / "manifest.json"
        if change == "identity":
            document = RootManifest.model_validate_json(path.read_bytes())
            changed = RootManifest.model_validate(
                {
                    **document.model_dump(),
                    "identity": StorageIdentity(
                        version=1, store_id="different-physical-store"
                    ),
                }
            )
            path.write_text(changed.model_dump_json())
        elif change == "malformed":
            path.write_bytes(b'{"version":1}')
        elif change == "oversized":
            path.write_bytes(b" " * 16385)
        else:
            path.chmod(0o644)

        with pytest.raises(KoraProtocolError) as refused:
            root.refresh_manifest()

    assert refused.value.code == code


def test_local_storage_refuses_unsafe_ancestors(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)

    with pytest.raises(KoraProtocolError) as refused:
        RootAnchor.open(unsafe / "store", StorageLimits(version=1))

    assert refused.value.code == "storage_path"


def test_local_storage_locks_use_independent_descriptions(tmp_path: Path) -> None:
    limits = StorageLimits(version=1)
    with RootAnchor.open(tmp_path / "store", limits) as first:
        with RootAnchor.open(tmp_path / "store", limits) as second:
            held = first.try_lock("root")
            assert held is not None

            blocked = second.try_lock("root")
            held.close()
            acquired = second.try_lock("root")

            assert blocked is None
            assert acquired is not None
            acquired.close()


def test_local_storage_payload_files_are_anchored_and_no_follow(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    with RootAnchor.open(tmp_path / "store", StorageLimits(version=1)) as root:
        os.symlink(outside, "intruder", dir_fd=root.fd)

        with pytest.raises(KoraProtocolError) as refused:
            root.open_file(("intruder",), os.O_RDONLY)

    assert refused.value.code == "storage_path"
    assert outside.read_bytes() == b"preserve"


def test_publication_closes_destination_when_source_directory_is_missing(
    tmp_path: Path,
) -> None:
    from kora.conformance.storage_support import content_address
    from kora.plugins.storage.content_files import ContentFiles
    from kora.plugins.storage.payload import FileSegment, PrivateFile

    with RootAnchor.open(tmp_path / "store", StorageLimits(version=1)) as root:
        address = content_address(root.identity, "sha256:" + "1" * 64)
        witness = FileSegment(
            ("missing", "part"),
            PrivateFile(
                version=1,
                private_identity="witness",
                device=root.manifest.device,
                inode=1,
            ),
            1,
        )
        before = descriptors_inside(tmp_path / "store")

        with pytest.raises(FileNotFoundError):
            ContentFiles(root).install(witness, address)

        assert descriptors_inside(tmp_path / "store") == before


def test_local_storage_refuses_replaced_live_anchor(tmp_path: Path) -> None:
    configured = tmp_path / "store"
    with RootAnchor.open(configured, StorageLimits(version=1)) as root:
        configured.rename(tmp_path / "old")
        configured.mkdir(mode=0o700)

        with pytest.raises(KoraProtocolError) as refused:
            root.validate_anchor()

    assert refused.value.code == "storage_identity"


def test_local_storage_never_recreates_a_missing_permanent_lock(tmp_path: Path) -> None:
    configured = tmp_path / "store"
    limits = StorageLimits(version=1)
    with RootAnchor.open(configured, limits) as root:
        held = root.try_lock("stage-0")
        assert held is not None
        (configured / "locks" / "stage-0").unlink()

        try:
            with pytest.raises(KoraProtocolError) as refused:
                RootAnchor.open(configured, limits)
        finally:
            held.close()

    assert refused.value.code == "storage_path"
    assert not (configured / "locks" / "stage-0").exists()


def test_local_storage_refuses_allow_acl_and_accepts_deny_only(tmp_path: Path) -> None:
    import struct
    import subprocess
    import sys

    unsafe = tmp_path / "acl-allow"
    safe = tmp_path / "acl-deny"
    unsafe.mkdir(mode=0o700)
    safe.mkdir(mode=0o700)
    if sys.platform == "darwin":
        subprocess.run(
            ["/bin/chmod", "+a", "everyone allow read", str(unsafe)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["/bin/chmod", "+a", "everyone deny delete", str(safe)],
            check=True,
            capture_output=True,
        )
    else:
        entries = (
            (1, 7, 2**32 - 1),
            (2, 4, os.geteuid() + 1),
            (4, 0, 2**32 - 1),
            (16, 0, 2**32 - 1),
            (32, 0, 2**32 - 1),
        )
        os.setxattr(
            unsafe,
            "system.posix_acl_access",
            struct.pack("<I", 2)
            + b"".join(struct.pack("<HHI", *entry) for entry in entries),
        )
        zero_entries = tuple(
            (tag, 0 if tag == 2 else permissions, uid)
            for tag, permissions, uid in entries
        )
        os.setxattr(
            safe,
            "system.posix_acl_access",
            struct.pack("<I", 2)
            + b"".join(struct.pack("<HHI", *entry) for entry in zero_entries),
        )

    with pytest.raises(KoraProtocolError) as refused:
        RootAnchor.open(unsafe / "store", StorageLimits(version=1))
    with RootAnchor.open(safe / "store", StorageLimits(version=1)) as root:
        identity = root.identity

    assert refused.value.code == "storage_path"
    assert identity.store_id
