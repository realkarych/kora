import errno
import os
import plistlib
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, StrictStr

from kora.plugins.runtime.local.bound_checkout import BoundCheckout
from kora.plugins.runtime.local.workspace_target import (
    MAX_OPERATION_NAMES,
    MAX_VISIT_NAMES,
    PhysicalFileKey,
    TargetRefusal,
    WorkspaceTarget,
    WorkspaceTargetError,
    WorkspaceTargetProof,
)
from kora.protocols.time import Clock, SystemClock
from kora.runner.filesystem import filesystem_kind
from tests.plugins.runtime.local.frames_support import StepClock
from tests.plugins.storage.pressure_images import DiskEntity
from tests.support.local_runtime import open_descriptors


def native_key(metadata: os.stat_result) -> PhysicalFileKey:
    return PhysicalFileKey(
        device=metadata.st_dev, inode=metadata.st_ino, owner_uid=metadata.st_uid
    )


def native_file_key(path: Path) -> PhysicalFileKey:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return native_key(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def checkout_path(parent: Path) -> Path:
    root = parent / "checkout"
    root.mkdir(mode=0o700, parents=True)
    return root


@contextmanager
def owned_target(
    root: Path,
    *,
    clock: Clock | None = None,
    cutoff: int | None = None,
    check_live: Callable[[], None] | None = None,
) -> Generator[WorkspaceTarget]:
    clock = SystemClock() if clock is None else clock
    held = BoundCheckout.capture(root)
    try:
        owner = WorkspaceTarget(
            held,
            clock.monotonic_ns() + 10_000_000_000 if cutoff is None else cutoff,
            clock,
            check_live=check_live,
        )
        try:
            yield owner
        finally:
            owner.close()
    finally:
        held.close()


def test_workspace_target_matches_independent_evidence_and_holds_descriptor(
    tmp_path: Path,
) -> None:
    root = checkout_path(tmp_path.resolve())
    (root / "a").mkdir()
    file = root / "a" / "actual name"
    file.write_bytes(b"\xff" * 8193)
    (root / "alias").symlink_to(file)
    (root / "a" / "relative").symlink_to("../a/actual name")
    physical = native_file_key(file)
    expected = WorkspaceTargetProof(
        lexical="alias", resolved="a/actual name", key=physical
    )
    before = open_descriptors()

    with owned_target(root) as owner:
        proof = owner.prepare(str(root / "alias"), expected=expected)
        held = owner.revalidate()
        opened = native_key(os.fstat(held))
        root_key = owner.root.key
    with owned_target(root) as owner:
        relative = owner.prepare("a/relative")
        relative_key = native_key(os.fstat(owner.revalidate()))

    assert proof == expected
    assert opened == physical
    assert root_key == native_key(root.stat())
    assert relative == WorkspaceTargetProof(
        lexical="a/relative",
        resolved="a/actual name",
        key=physical,
    )
    assert relative_key == physical
    assert open_descriptors() == before
    with pytest.raises(OSError, match=os.strerror(errno.EBADF)) as closed:
        os.fstat(held)
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("change", ["name", "resolved", "device", "inode", "owner"])
def test_workspace_target_refuses_mismatched_evidence_and_releases(
    tmp_path: Path, change: str
) -> None:
    root = checkout_path(tmp_path.resolve())
    file = root / "file"
    file.touch()
    native = native_file_key(file)
    key = PhysicalFileKey(
        device=native.device + (change == "device"),
        inode=native.inode + (change == "inode"),
        owner_uid=native.owner_uid + (change == "owner"),
    )
    expected = WorkspaceTargetProof(
        lexical="different" if change == "name" else "file",
        resolved="different" if change == "resolved" else "file",
        key=key,
    )

    with owned_target(root) as owner:
        before = open_descriptors()
        with pytest.raises(WorkspaceTargetError) as refusal:
            owner.prepare("file", expected=expected)

        assert refusal.value.reason is TargetRefusal.BINDING
        assert open_descriptors() == before
        with pytest.raises(WorkspaceTargetError) as consumed:
            owner.prepare("file")
        assert consumed.value.reason is TargetRefusal.CLOSED


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("target", TargetRefusal.BINDING),
        ("link", TargetRefusal.BINDING),
        ("root", TargetRefusal.ROOT),
        ("ancestor", TargetRefusal.ROOT),
        ("size", TargetRefusal.FRESHNESS),
        ("mtime", TargetRefusal.FRESHNESS),
    ],
)
def test_workspace_target_rechecks_root_lineage_binding_and_freshness(
    tmp_path: Path, change: str, reason: TargetRefusal
) -> None:
    parent = tmp_path.resolve()
    outer = parent / "outer"
    root = checkout_path(outer)
    target = root / "file"
    target.write_bytes(b"before")
    alias = root / "alias"
    alias.symlink_to("file")
    original = native_file_key(target)
    original_stamp = target.stat()
    original_root = native_key(root.stat())
    original_outer = native_key(outer.stat())

    with owned_target(root) as owner:
        unheld_count = open_descriptors()
        prepared = owner.prepare("alias")
        if change == "target":
            target.rename(root / "old")
            target.touch()
        elif change == "link":
            alias.rename(root / "old-link")
            alias.symlink_to("./file")
        elif change == "root":
            root.rename(outer / "old")
            checkout_path(outer)
            target.touch()
        elif change == "ancestor":
            saved = parent / "old-outer"
            outer.rename(saved)
            outer.mkdir()
            (saved / root.name).rename(root)
        elif change == "size":
            target.write_bytes(b"after preparation")
        else:
            os.utime(
                target,
                ns=(
                    original_stamp.st_atime_ns,
                    original_stamp.st_mtime_ns + 2_000_000_000,
                ),
            )
        changed = target.stat()
        with pytest.raises(WorkspaceTargetError) as refused:
            owner.revalidate()

        assert open_descriptors() == unheld_count

    assert prepared.key == original
    assert refused.value.reason is reason
    if change in ("ancestor", "link", "size", "mtime"):
        assert native_key(changed) == original
    if change == "target":
        assert native_key(changed) != original
    if change == "size":
        assert changed.st_size != original_stamp.st_size
    if change == "mtime":
        assert changed.st_size == original_stamp.st_size
        assert changed.st_mtime_ns != original_stamp.st_mtime_ns
    if change == "root":
        assert native_key(root.stat()) != original_root
    if change == "ancestor":
        assert native_key(root.stat()) == original_root
        assert native_key(outer.stat()) != original_outer


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/not-the-checkout/file",
        "a/",
        "a//b",
        "a/./b",
        "a/../b",
        " file",
        "file ",
        "@file",
        "~file",
        "fi\t le",
        "a\u0085b",
        "a\u00a0b",
        "a\x00b",
        "\ufefffile",
        "a\ud800b",
        "é" * 128,
        "a/" * 16 + "leaf",
        "/".join(["a" * 255] * 5),
    ],
)
def test_workspace_target_refuses_unadmitted_spellings(
    tmp_path: Path, path: str
) -> None:
    root = checkout_path(tmp_path.resolve())

    with owned_target(root) as owner, pytest.raises(WorkspaceTargetError) as refused:
        owner.prepare(path)

    assert refused.value.reason is TargetRefusal.PATH


@pytest.mark.parametrize("character", ["\t", "\u0085", "\u00a0", "\ufeff"])
def test_workspace_target_checks_the_entire_authored_absolute_spelling(
    tmp_path: Path, character: str
) -> None:
    root = checkout_path(tmp_path.resolve() / f"first{character}second")
    path = root / "file"
    path.touch()
    expected = native_file_key(path)

    with owned_target(root) as owner, pytest.raises(WorkspaceTargetError) as refused:
        owner.prepare(str(path))
    with owned_target(root) as owner:
        relative = owner.prepare(path.name)
        actual = native_key(os.fstat(owner.revalidate()))

    assert refused.value.reason is TargetRefusal.PATH
    assert relative.key == actual == expected


def test_workspace_target_rejects_physical_escape_and_nonregular_objects(
    tmp_path: Path,
) -> None:
    parent = tmp_path.resolve()
    root = checkout_path(parent)
    outside = parent / "outside"
    outside.touch()
    (root / "escape").symlink_to("../outside")
    (root / "absolute").symlink_to(outside)
    (root / "cycle").symlink_to("cycle")
    os.link(outside, root / "hard")
    os.mkfifo(root / "fifo")
    (root / "directory").mkdir()
    before = open_descriptors()

    reasons: list[TargetRefusal] = []
    for name in ("escape", "absolute", "cycle", "hard", "fifo", "directory", "missing"):
        with (
            owned_target(root) as owner,
            pytest.raises(WorkspaceTargetError) as refused,
        ):
            owner.prepare(name)
        reasons.append(refused.value.reason)

    assert reasons == [TargetRefusal.PATH] * 6 + [TargetRefusal.SPELLING]
    assert open_descriptors() == before


def test_workspace_target_bounds_symlink_expansion_and_descriptor_ownership(
    tmp_path: Path,
) -> None:
    root = checkout_path(tmp_path.resolve())
    names = tuple(f"d{i}" for i in range(15))
    deepest = root.joinpath(*names)
    deepest.mkdir(parents=True)
    file = deepest / "terminal"
    file.touch()
    for index in range(9):
        (deepest / f"alias{index}").symlink_to(
            "terminal" if index == 8 else f"alias{index + 1}"
        )
    admitted = "/".join((*names, "alias1"))
    refused = "/".join((*names, "alias0"))
    before = open_descriptors()
    observed_max = before

    def measure_descriptors() -> None:
        nonlocal observed_max
        observed_max = max(observed_max, open_descriptors())

    with owned_target(root, check_live=measure_descriptors) as owner:
        anchored_count = open_descriptors()
        target = owner.prepare(admitted)
        prepared_count = open_descriptors()
        descriptor = owner.revalidate()
        revalidated_count = open_descriptors()
    with owned_target(root) as owner, pytest.raises(WorkspaceTargetError) as excess:
        owner.prepare(refused)

    assert target.resolved == "/".join((*names, "terminal"))
    assert target.key == native_file_key(file)
    assert excess.value.reason is TargetRefusal.PATH
    assert prepared_count == anchored_count + 1
    assert revalidated_count == prepared_count
    assert anchored_count < observed_max <= before + 24
    assert open_descriptors() == before
    with pytest.raises(OSError, match=os.strerror(errno.EBADF)) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


def test_workspace_target_refuses_at_inherited_cutoff(tmp_path: Path) -> None:
    root = checkout_path(tmp_path.resolve())
    (root / "file").touch()
    clock = StepClock()
    cutoff = clock.monotonic_ns() + 100

    with owned_target(root, clock=clock, cutoff=cutoff) as owner:
        before = open_descriptors()
        owner.prepare("file")
        clock.advance(cutoff - clock.monotonic_ns())
        with pytest.raises(WorkspaceTargetError) as refused:
            owner.revalidate()

        assert open_descriptors() == before

    assert clock.monotonic_ns() == cutoff
    assert refused.value.reason is TargetRefusal.DEADLINE


def native_checkout_ancestry(parent: Path, names: tuple[str, ...]) -> Path:
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in names:
            os.mkdir(name, mode=0o700, dir_fd=descriptor)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
            closing, descriptor = descriptor, child
            os.close(closing)
    finally:
        os.close(descriptor)
    return parent.joinpath(*names)


@pytest.mark.parametrize("limit", ["ancestors", "bytes"])
def test_workspace_target_bounds_pwd_before_observing_paths(
    tmp_path: Path, limit: str
) -> None:
    parent = tmp_path.resolve()
    parts = len(parent.parts) - 1
    names = ("d",) * (33 - parts) if limit == "ancestors" else ("d" * 255,) * 16
    root = native_checkout_ancestry(parent, names)
    assert (len(root.parts) - 1 > 32) == (limit == "ancestors")
    assert (len(str(root).encode()) > 4096) == (limit == "bytes")
    clock = StepClock()
    observations: list[int] = []
    checkout = BoundCheckout.capture(root)
    current = open_descriptors()

    try:
        with pytest.raises(WorkspaceTargetError) as refused:
            WorkspaceTarget(
                checkout,
                clock.monotonic_ns() + 100,
                clock,
                check_live=lambda: observations.append(open_descriptors()),
            )
        after = open_descriptors()
    finally:
        checkout.close()

    assert refused.value.reason is TargetRefusal.ROOT
    assert not observations
    assert after == current


def native_names(directory: Path) -> tuple[str, ...]:
    with os.scandir(directory) as entries:
        return tuple(entry.name for entry in entries)


def fill_files(directory: Path, count: int) -> tuple[str, ...]:
    for index in range(count):
        (directory / f"entry-{index:05}").touch()
    return native_names(directory)


def test_workspace_target_bounds_exact_native_visit(tmp_path: Path) -> None:
    root = checkout_path(tmp_path.resolve())
    names = fill_files(root, 4097)
    assert len(names) == 4097
    exact, excess = names[4095:]

    with owned_target(root) as owner:
        proof = owner.prepare(exact)
        confirmed = native_key(os.fstat(owner.revalidate()))
    with owned_target(root) as owner, pytest.raises(WorkspaceTargetError) as refused:
        owner.prepare(excess)

    assert MAX_VISIT_NAMES == 4096
    assert proof.key == confirmed == native_file_key(root / exact)
    assert refused.value.reason is TargetRefusal.CAPACITY


def count_ancestry_names(root: Path) -> int:
    parent = Path("/")
    total = 0
    for name in root.parts[1:]:
        total += native_names(parent).index(name) + 1
        parent /= name
    return total


def test_workspace_target_bounds_two_walks_and_ancestry_together(
    tmp_path: Path,
) -> None:
    root = checkout_path(tmp_path.resolve())
    remaining = 8192 - count_ancestry_names(root)
    assert remaining > 0
    checked = root
    components: list[str] = []
    while remaining >= 4096:
        for index in range(4096):
            (checked / f"entry-{index:05}").mkdir()
        name = native_names(checked)[4094]
        components.append(name)
        checked /= name
        remaining -= 4095
    assert remaining > 0
    names = fill_files(checked, remaining + 1)
    exact, excess = names[-2:]
    good = "/".join((*components, exact))
    over = "/".join((*components, excess))
    exact_cost = 2 * (count_ancestry_names(root) + 4095 * len(components) + remaining)

    with owned_target(root) as owner:
        proof = owner.prepare(good)
        confirmed = native_key(os.fstat(owner.revalidate()))
    with owned_target(root) as owner:
        before = open_descriptors()
        submitted = owner.prepare(over)
        with pytest.raises(WorkspaceTargetError) as refused:
            owner.revalidate()

        assert open_descriptors() == before

    assert MAX_OPERATION_NAMES == exact_cost == 16384
    assert confirmed == proof.key == native_file_key(checked / exact)
    assert submitted.key == native_file_key(checked / excess)
    assert refused.value.reason is TargetRefusal.CAPACITY


def hdiutil(*args: str) -> bytes:
    result = subprocess.run(
        ("/usr/bin/hdiutil", *args),
        check=True,
        capture_output=True,
        timeout=30,
    )
    return result.stdout


class ApfsEntity(DiskEntity):
    content_hint: StrictStr | None = Field(default=None, alias="content-hint")


class ApfsImage(BaseModel):
    path: StrictStr | None = Field(default=None, alias="image-path")
    entities: tuple[ApfsEntity, ...] = Field(default=(), alias="system-entities")


class ApfsImages(BaseModel):
    images: tuple[ApfsImage, ...] = ()


def attached_image(image: Path) -> ApfsImage | None:
    payload: object = plistlib.loads(hdiutil("info", "-plist"))
    inventory = ApfsImages.model_validate(payload)
    matches = tuple(
        item
        for item in inventory.images
        if item.path is not None and Path(item.path).resolve() == image
    )
    assert len(matches) <= 1
    return matches[0] if matches else None


def whole_image_device(attachment: ApfsImage) -> str:
    (device,) = (
        entity.device
        for entity in attachment.entities
        if entity.content_hint == "GUID_partition_scheme"
    )
    assert device is not None
    assert re.fullmatch(r"/dev/disk[0-9]{1,8}", device)
    assert stat.S_ISBLK(os.stat(device).st_mode)
    return device


def image_mount_device(attachment: ApfsImage, mount: Path) -> str:
    volumes = tuple(
        entity for entity in attachment.entities if entity.mount_point is not None
    )
    assert len(volumes) == 1, "expected a single APFS mount"
    (volume,) = volumes
    device = volume.device
    assert device is not None
    assert re.fullmatch(r"/dev/disk[0-9]{1,8}s[0-9]{1,4}", device)
    assert volume.mount_point == str(mount), (
        "APFS mount is outside its expected location"
    )
    native = os.stat(device)
    assert stat.S_ISBLK(native.st_mode)
    assert native.st_rdev == mount.stat().st_dev
    return device


def apfs_aliases(directory: Path) -> bool:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if filesystem_kind(descriptor) != "apfs":
            return False
    finally:
        os.close(descriptor)
    spelling = directory / "Kora-Évidence"
    spelling.touch()
    try:
        return native_file_key(spelling) == native_file_key(
            directory / "kora-E\u0301vidence"
        )
    except FileNotFoundError:
        return False
    finally:
        spelling.unlink()


def attach_apfs_volume(image: Path, mount: Path) -> None:
    hdiutil(
        "attach", "-nobrowse", "-owners", "on", "-mountpoint", str(mount), str(image)
    )


@contextmanager
def owned_apfs_image(parent: Path) -> Generator[tuple[Path, Path]]:
    image = parent / "target.dmg"
    mount = parent / "mount"
    mount.mkdir()
    try:
        hdiutil(
            "create",
            "-size",
            "96m",
            "-layout",
            "GPTSPUD",
            "-fs",
            "APFS",
            "-type",
            "UDIF",
            str(image),
        )
        yield image, mount
    finally:
        live = attached_image(image)
        if live is not None:
            hdiutil("detach", whole_image_device(live))
            for entity in live.entities:
                if entity.mount_point is not None:
                    assert not os.path.ismount(entity.mount_point)
        assert attached_image(image) is None
        assert not os.path.ismount(mount)
        image.unlink(missing_ok=True)


@contextmanager
def owned_apfs_volume(parent: Path) -> Generator[Path]:
    with owned_apfs_image(parent) as (image, mount):
        attach_apfs_volume(image, mount)
        live = attached_image(image)
        assert live is not None
        image_mount_device(live, mount)
        assert os.path.ismount(mount)
        assert apfs_aliases(mount)
        yield mount.resolve()


@contextmanager
def native_spelling_workspace(parent: Path, disposable: bool) -> Generator[Path]:
    if disposable or (sys.platform == "darwin" and not apfs_aliases(parent)):
        with owned_apfs_volume(parent) as mount:
            yield checkout_path(mount)
    else:
        yield checkout_path(parent)


if sys.platform == "darwin":

    @pytest.mark.parametrize("mounted", [False, True])
    def test_workspace_target_releases_whole_apfs_image_after_mount_refusal(
        tmp_path: Path,
        mounted: bool,
    ) -> None:
        parent = tmp_path.resolve()
        failure = "APFS mount is outside" if mounted else "expected a single APFS mount"

        with owned_apfs_image(parent) as (image, mount):
            if mounted:
                attach_apfs_volume(image, mount)
            else:
                hdiutil("attach", "-nomount", "-plist", str(image))
            live = attached_image(image)
            assert live is not None
            assert os.path.ismount(mount) == mounted
            with pytest.raises(AssertionError, match=failure):
                image_mount_device(live, parent / "unaccepted-mount")

        assert attached_image(image) is None
        assert not os.path.ismount(mount)
        assert not image.exists()


@pytest.mark.parametrize(
    "disposable", [False, True] if sys.platform == "darwin" else [False]
)
def test_workspace_target_checks_actual_case_and_unicode_spelling(
    tmp_path: Path,
    disposable: bool,
) -> None:
    with native_spelling_workspace(tmp_path.resolve(), disposable) as root:
        old = root / "target"
        renamed = root / "Target"
        old.touch()
        (root / "café").touch()
        stored = next(
            name
            for name in native_names(root)
            if unicodedata.normalize("NFC", name) == "café"
        )
        normalized = unicodedata.normalize("NFD" if stored == "café" else "NFC", stored)
        old_key = native_file_key(old)
        stored_key = native_file_key(root / stored)

        with owned_target(root) as owner:
            owner.prepare(old.name)
            old.rename(renamed)
            assert old.name not in native_names(root)
            assert renamed.name in native_names(root)
            if sys.platform == "darwin":
                assert native_file_key(old) == native_file_key(renamed) == old_key
            with pytest.raises(WorkspaceTargetError) as case:
                owner.revalidate()
        with owned_target(root) as owner:
            proved = owner.prepare(stored)
            confirmed_key = native_key(os.fstat(owner.revalidate()))
        if sys.platform == "darwin":
            assert normalized != stored
            assert native_file_key(root / normalized) == stored_key
        with (
            owned_target(root) as owner,
            pytest.raises(WorkspaceTargetError) as unicode_alias,
        ):
            owner.prepare(normalized)

        assert native_file_key(renamed) == old_key
        assert case.value.reason is TargetRefusal.SPELLING
        assert proved.lexical == proved.resolved == stored
        assert proved.key == confirmed_key == stored_key
        assert unicode_alias.value.reason is TargetRefusal.SPELLING
