import ctypes
import errno
import fcntl
import os
import struct
import sys
from typing import Final

from kora.plugins.storage.errors import refuse

LIBC: Final[ctypes.CDLL] = ctypes.CDLL(None, use_errno=True)


def synchronize_file(fd: int) -> None:
    os.fsync(fd)
    if sys.platform == "darwin":
        fcntl.fcntl(fd, 51)


def filesystem(fd: int) -> tuple[str, str]:
    state = ctypes.create_string_buffer(4096)
    inspect = LIBC.fstatfs
    inspect.argtypes = [ctypes.c_int, ctypes.c_void_p]
    inspect.restype = ctypes.c_int
    if inspect(fd, state) != 0:
        refuse("storage_path")
    if sys.platform == "darwin":
        name = state.raw[72:88].split(b"\0", 1)[0].decode("ascii")
        if name not in {"apfs", "hfs"}:
            refuse("storage_path")
        return name, state.raw[48:56].hex()
    if sys.platform != "linux" or struct.unpack_from("@l", state.raw)[0] != 0xEF53:
        refuse("storage_path")
    device = os.fstat(fd).st_dev
    selected = f"{os.major(device)}:{os.minor(device)}".encode()
    with open("/proc/self/mountinfo", "rb") as source:
        mountinfo = source.read(1048577)
    if len(mountinfo) > 1048576:
        refuse("storage_path")
    types = {
        line.split(b" - ", 1)[1].split(b" ", 1)[0]
        for line in mountinfo.splitlines()
        if len(line.split(b" ", 3)) >= 4
        and line.split(b" ", 3)[2] == selected
        and b" - " in line
    }
    if types != {b"ext4"}:
        refuse("storage_path")
    offset = 7 * ctypes.sizeof(ctypes.c_long)
    return "ext4", state.raw[offset : offset + 8].hex()


def validate_acl(fd: int) -> None:
    if sys.platform == "darwin":
        validate_darwin_acl(fd)
        return
    if sys.platform != "linux":
        refuse("storage_path")
    for name in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            data = os.getxattr(fd, name)
        except OSError as error:
            if error.errno != errno.ENODATA:
                refuse("storage_path")
            continue
        if len(data) < 4 or len(data) > 16384 or (len(data) - 4) % 8:
            refuse("storage_path")
        if struct.unpack_from("<I", data)[0] != 2:
            refuse("storage_path")
        for tag, permissions, _ in struct.iter_unpack("<HHI", data[4:]):
            if tag not in {1, 2, 4, 8, 16, 32} or (tag in {2, 8} and permissions):
                refuse("storage_path")


def validate_darwin_acl(fd: int) -> None:
    get_acl = LIBC.acl_get_fd_np
    get_acl.argtypes = [ctypes.c_int, ctypes.c_int]
    get_acl.restype = ctypes.c_void_p
    free_acl = LIBC.acl_free
    free_acl.argtypes = [ctypes.c_void_p]
    free_acl.restype = ctypes.c_int
    get_entry = LIBC.acl_get_entry
    get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    get_entry.restype = ctypes.c_int
    get_tag = LIBC.acl_get_tag_type
    get_tag.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    get_tag.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = get_acl(fd, 0x100)
    if acl is None:
        if ctypes.get_errno() == errno.ENOENT:
            os.fstat(fd)
            return
        refuse("storage_path")
    try:
        for index in range(170):
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            if get_entry(acl, index, ctypes.byref(entry)) != 0:
                if ctypes.get_errno() != errno.EINVAL:
                    refuse("storage_path")
                return
            tag = ctypes.c_int()
            if get_tag(entry, ctypes.byref(tag)) != 0 or tag.value != 2:
                refuse("storage_path")
        refuse("storage_path")
    finally:
        if free_acl(acl) != 0:
            refuse("storage_path")
