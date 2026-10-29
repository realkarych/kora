import os
import plistlib
import re
import stat
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from kora.plugins.storage.native import filesystem
from tests.plugins.storage.pressure_holders import Binding, capture_binding
from tests.plugins.storage.pressure_models import ImageStamp, PressureFailure
from tests.plugins.storage.pressure_native import command

Effect = Callable[[tuple[str, ...]], bytes]
Observe = Callable[[Binding], str]


class LoopDevice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: StrictStr
    backing_file: StrictStr | None = Field(default=None, alias="back-file")
    backing_inode: StrictInt | None = Field(default=None, alias="back-ino")
    backing_device: StrictStr | None = Field(default=None, alias="back-maj:min")


class LoopDevices(BaseModel):
    loopdevices: tuple[LoopDevice, ...]


class DiskEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    device: StrictStr | None = Field(default=None, alias="dev-entry")
    mount_point: StrictStr | None = Field(default=None, alias="mount-point")


class DiskImage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: StrictStr | None = Field(default=None, alias="image-path")
    entities: tuple[DiskEntity, ...] = Field(default=(), alias="system-entities")


class DiskImages(BaseModel):
    model_config = ConfigDict(extra="ignore")
    images: tuple[DiskImage, ...] = ()


@dataclass(frozen=True)
class Attachment:
    device: str
    volume: str | None
    mounted: bool
    entities: tuple[str, ...] = ()


def image_name() -> str:
    return "filesystem.dmg" if sys.platform == "darwin" else "filesystem.img"


def image_identity(
    information: os.stat_result,
    expected: ImageStamp | None,
    permissions: tuple[int, ...] = (0o600,),
) -> ImageStamp:
    if (
        not stat.S_ISREG(information.st_mode)
        or information.st_uid != os.geteuid()
        or stat.S_IMODE(information.st_mode) not in permissions
        or information.st_nlink != 1
        or not 0 <= information.st_size <= 68157440
    ):
        raise PressureFailure("image_identity")
    actual = ImageStamp(device=information.st_dev, inode=information.st_ino)
    if expected is not None and actual != expected:
        raise PressureFailure("image_identity")
    return actual


def inspect_image(path: Path, expected: ImageStamp | None) -> ImageStamp:
    return image_identity(path.lstat(), expected)


def prepare_image(directory: Path) -> ImageStamp:
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        information = os.fstat(parent)
        if (
            information.st_uid != os.geteuid()
            or stat.S_IMODE(information.st_mode) != 0o700
            or (information.st_dev, information.st_ino)
            != (directory.stat().st_dev, directory.stat().st_ino)
        ):
            raise PressureFailure("directory_identity")
        descriptor = os.open(
            image_name(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            stamp = image_identity(os.fstat(descriptor), None, (0o600, 0o644))
            os.fchmod(descriptor, 0o600)
            image_identity(os.fstat(descriptor), stamp)
            inspect_image(directory / image_name(), stamp)
            return stamp
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def create_image(directory: Path, nonce: str, effect: Effect) -> ImageStamp:
    image = directory / image_name()
    if sys.platform == "darwin":
        effect(
            (
                "/usr/bin/hdiutil",
                "create",
                "-size",
                "64m",
                "-fs",
                "HFS+",
                "-volname",
                "kora-pressure-" + nonce[:12],
                "-o",
                str(image),
            )
        )
        return prepare_image(directory)
    descriptor = os.open(
        image, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        os.ftruncate(descriptor, 67108864)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    effect(
        (
            "/usr/sbin/mkfs.ext4",
            "-q",
            "-F",
            "-U",
            str(uuid.UUID(hex=nonce)),
            "-E",
            f"root_owner={os.geteuid()}:{os.getegid()}",
            str(image),
        )
    )
    return inspect_image(image, None)


def linux_attachment(directory: Path, stamp: ImageStamp) -> Attachment | None:
    inventory = LoopDevices.model_validate_json(
        command(
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/sbin/losetup",
                "--json",
                "--list",
                "--output",
                "NAME,BACK-FILE,BACK-INO,BACK-MAJ:MIN",
            )
        )
    )
    image = str(directory / image_name())
    device = f"{os.major(stamp.device)}:{os.minor(stamp.device)}"
    selected = tuple(
        item
        for item in inventory.loopdevices
        if item.backing_file == image
        or (item.backing_inode == stamp.inode and item.backing_device == device)
    )
    if not selected:
        return None
    if (
        len(selected) != 1
        or selected[0].backing_inode != stamp.inode
        or selected[0].backing_device != device
        or re.fullmatch(r"/dev/loop[0-9]{1,8}", selected[0].name) is None
    ):
        raise PressureFailure("attachment_identity")
    loop = selected[0].name
    mounted = os.path.ismount(directory / "mounted")
    if mounted:
        info = os.stat(loop)
        if (
            not stat.S_ISBLK(info.st_mode)
            or os.stat(directory / "mounted").st_dev != info.st_rdev
        ):
            raise PressureFailure("mount_identity")
        mountinfo = Path("/proc/self/mountinfo").read_bytes()
        if len(mountinfo) > 1048576:
            raise PressureFailure("mount_inventory_limit")
        encoded = os.fsencode(directory / "mounted")
        matches = tuple(
            line.split()
            for line in mountinfo.splitlines()
            if len(line.split()) > 6
            and line.split()[4].replace(b"\\040", b" ").replace(b"\\134", b"\\")
            == encoded
        )
        if len(matches) != 1 or not {b"nosuid", b"nodev", b"noexec"}.issubset(
            set(matches[0][5].split(b","))
        ):
            raise PressureFailure("mount_options")
    return Attachment(loop, loop, mounted)


def darwin_attachment(directory: Path) -> Attachment | None:
    raw: object = plistlib.loads(command(("/usr/bin/hdiutil", "info", "-plist")))
    inventory = DiskImages.model_validate(raw)
    image = str(directory / image_name())
    selected = tuple(
        item
        for item in inventory.images
        if item.path is not None and str(Path(item.path).resolve()) == image
    )
    if not selected:
        return None
    if len(selected) != 1:
        raise PressureFailure("attachment_identity")
    roots = tuple(
        item.device
        for item in selected[0].entities
        if item.device is not None and re.fullmatch(r"/dev/disk[0-9]{1,8}", item.device)
    )
    volumes = tuple(
        item for item in selected[0].entities if item.mount_point is not None
    )
    if len(roots) != 1 or len(volumes) > 1:
        raise PressureFailure("attachment_identity")
    volume = None if not volumes else volumes[0].device
    if volumes:
        if (
            volumes[0].mount_point != str(directory / "mounted")
            or volume is None
            or re.fullmatch(re.escape(roots[0]) + r"(?:s[0-9]{1,4}){0,2}", volume)
            is None
        ):
            raise PressureFailure("mount_identity")
        info = os.stat(volume)
        if (
            not stat.S_ISBLK(info.st_mode)
            or os.stat(directory / "mounted").st_dev != info.st_rdev
        ):
            raise PressureFailure("mount_identity")
    entities = tuple(
        item.device
        for item in selected[0].entities
        if item.device is not None
        and re.fullmatch(re.escape(roots[0]) + r"(?:s[0-9]{1,4}){0,2}", item.device)
    )
    return Attachment(roots[0], volume, bool(volumes), entities)


def discover(directory: Path, stamp: ImageStamp) -> Attachment | None:
    inspect_image(directory / image_name(), stamp)
    return (
        darwin_attachment(directory)
        if sys.platform == "darwin"
        else linux_attachment(directory, stamp)
    )


def attach(directory: Path, effect: Effect) -> None:
    if sys.platform == "darwin":
        effect(
            (
                "/usr/bin/hdiutil",
                "attach",
                "-nobrowse",
                "-owners",
                "on",
                "-mountpoint",
                str(directory / "mounted"),
                "-plist",
                str(directory / image_name()),
            )
        )
    else:
        effect(
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/sbin/losetup",
                "--find",
                "--show",
                "--nooverlap",
                str(directory / image_name()),
            )
        )


def mount(directory: Path, attachment: Attachment, effect: Effect) -> None:
    if sys.platform == "linux":
        effect(
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/mount",
                "-t",
                "ext4",
                "-o",
                "nosuid,nodev,noexec",
                attachment.device,
                str(directory / "mounted"),
            )
        )


def verify_mount(directory: Path, attachment: Attachment) -> str:
    if not attachment.mounted:
        raise PressureFailure("mount_missing")
    descriptor = os.open(
        directory / "mounted", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        information = os.fstat(descriptor)
        if attachment.volume is None:
            raise PressureFailure("mount_identity")
        device = os.stat(attachment.volume)
        if (
            not stat.S_ISBLK(device.st_mode)
            or information.st_dev != device.st_rdev
            or information.st_uid != os.geteuid()
            or not stat.S_ISDIR(information.st_mode)
        ):
            raise PressureFailure("mount_identity")
        name, _ = filesystem(descriptor)
        expected = "hfs" if sys.platform == "darwin" else "ext4"
        if name != expected:
            raise PressureFailure("filesystem_identity")
        if sys.platform == "darwin":
            if stat.S_IMODE(information.st_mode) not in (0o700, 0o755):
                raise PressureFailure("mount_permissions")
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            updated = os.fstat(descriptor)
            if (
                updated.st_uid != information.st_uid
                or stat.S_IMODE(updated.st_mode) != 0o700
                or (updated.st_dev, updated.st_ino)
                != (information.st_dev, information.st_ino)
            ):
                raise PressureFailure("mount_identity")
        anchored = (directory / "mounted").lstat()
        if (anchored.st_dev, anchored.st_ino) != (
            information.st_dev,
            information.st_ino,
        ):
            raise PressureFailure("mount_identity")
    finally:
        os.close(descriptor)
    return expected


def detach(
    directory: Path,
    stamp: ImageStamp,
    effect: Effect,
    observe: Observe,
    owner_device: int,
) -> None:
    owned = discover(directory, stamp)
    if owned is None:
        return
    if sys.platform == "darwin":
        binding = capture_binding(
            directory / "mounted", owned.volume, owned.entities, owner_device
        )
        try:
            effect(("/usr/bin/hdiutil", "detach", owned.device))
        except PressureFailure as failure:
            if failure.code != "command_failed":
                raise
            failure.add_note(observe(binding))
            raise
    else:
        if owned.mounted:
            effect(
                ("/usr/bin/sudo", "-n", "/usr/bin/umount", str(directory / "mounted"))
            )
        checked = discover(directory, stamp)
        if checked is None or checked.device != owned.device or checked.mounted:
            raise PressureFailure("attachment_identity")
        effect(("/usr/bin/sudo", "-n", "/usr/sbin/losetup", "--detach", owned.device))
    if discover(directory, stamp) is not None:
        raise PressureFailure("attachment_still_present")
