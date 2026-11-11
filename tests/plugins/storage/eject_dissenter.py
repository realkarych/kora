import ctypes
import os
import sys
import threading
from typing import cast

from tests.plugins.storage.pressure_models import PressureFailure

BUSY_STATUS = 0xC010
UTF8 = 0x08000100
ITERATE_PARENTS_RECURSIVELY = 3
PATH_LIMIT = 4096


class Decisions:
    def __init__(self, image: bytes) -> None:
        self.image: bytes = image
        self.observing: bool = False
        self.refused: bool = False
        self.output: threading.Lock = threading.Lock()

    def emit(self, line: bytes) -> None:
        with self.output:
            sys.stdout.buffer.write(line + b"\n")
            sys.stdout.buffer.flush()

    def observe(self) -> None:
        self.observing = True
        self.emit(b"observing")

    def refuses(self, image: bytes | None) -> bool:
        if image != self.image:
            return False
        if self.observing:
            self.emit(b"observed")
            return False
        if self.refused:
            self.emit(b"approved")
            return False
        self.refused = True
        self.emit(b"refused")
        return True


def main() -> None:
    if sys.platform != "darwin":
        raise PressureFailure("native_platform")
    decisions = Decisions(os.fsencode(os.path.realpath(sys.argv[1])))
    root = "/System/Library/Frameworks/"
    cf = ctypes.CDLL(root + "CoreFoundation.framework/CoreFoundation")
    da = ctypes.CDLL(root + "DiskArbitration.framework/DiskArbitration")
    iokit = ctypes.CDLL(root + "IOKit.framework/IOKit")
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    ref = ctypes.c_void_p
    port = ctypes.c_uint32
    ffi = ctypes.CFUNCTYPE
    approval_type = ffi(ref, ref, ref)
    release = ffi(None, ref)(("CFRelease", cf))
    type_of = ffi(ctypes.c_ulong, ref)(("CFGetTypeID", cf))
    data_type = cast(int, ffi(ctypes.c_ulong)(("CFDataGetTypeID", cf))())
    string_type = cast(int, ffi(ctypes.c_ulong)(("CFStringGetTypeID", cf))())
    data_length = ffi(ctypes.c_long, ref)(("CFDataGetLength", cf))
    data_bytes = ffi(ref, ref)(("CFDataGetBytePtr", cf))
    string_bytes = ffi(
        ctypes.c_ubyte, ref, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
    )(("CFStringGetCString", cf))
    create_string = ffi(ref, ref, ctypes.c_char_p, ctypes.c_uint32)(
        ("CFStringCreateWithCString", cf)
    )
    create_session = ffi(ref, ref)(("DASessionCreate", da))
    set_queue = ffi(None, ref, ref)(("DASessionSetDispatchQueue", da))
    register = ffi(None, ref, ref, approval_type, ref)(
        ("DARegisterDiskEjectApprovalCallback", da)
    )
    unregister = ffi(None, ref, approval_type, ref)(
        ("DAUnregisterApprovalCallback", da)
    )
    dissent = ffi(ref, ref, ctypes.c_uint32, ref)(("DADissenterCreate", da))
    copy_media = ffi(port, ref)(("DADiskCopyIOMedia", da))
    search = ffi(ref, port, ctypes.c_char_p, ref, ref, ctypes.c_uint32)(
        ("IORegistryEntrySearchCFProperty", iokit)
    )
    release_media = ffi(ctypes.c_int, port)(("IOObjectRelease", iokit))
    create_queue = ffi(ref, ctypes.c_char_p, ref)(("dispatch_queue_create", system))
    key = cast(int | None, create_string(None, b"image-path", UTF8))
    session = cast(int | None, create_session(None))
    queue = cast(int | None, create_queue(b"kora.eject-dissenter", None))
    if key is None or session is None or queue is None:
        raise PressureFailure("native_session_missing")

    def decoded(value: int) -> bytes | None:
        kind = cast(int, type_of(value))
        if kind == data_type:
            length = cast(int, data_length(value))
            start = cast(int | None, data_bytes(value))
            if start is None or not 0 < length <= PATH_LIMIT:
                return None
            return ctypes.string_at(start, length).rstrip(b"\x00")
        if kind == string_type:
            buffer = ctypes.create_string_buffer(PATH_LIMIT + 1)
            if not string_bytes(value, buffer, len(buffer), UTF8):
                return None
            return buffer.value
        return None

    def image_path(disk: int | None) -> bytes | None:
        media = 0 if disk is None else cast(int, copy_media(disk))
        if media == 0:
            return None
        try:
            value = cast(
                int | None,
                search(media, b"IOService", key, None, ITERATE_PARENTS_RECURSIVELY),
            )
        finally:
            release_media(media)
        if value is None:
            return None
        try:
            return decoded(value)
        finally:
            release(value)

    def approve(disk: int | None, context: int | None) -> int | None:
        try:
            refused = decisions.refuses(image_path(disk))
        except BrokenPipeError:
            return None
        return cast(int | None, dissent(None, BUSY_STATUS, None)) if refused else None

    callback = approval_type(approve)
    set_queue(session, queue)
    register(session, None, callback, None)
    try:
        decisions.emit(b"ready")
        while sys.stdin.buffer.read(1) == b"o":
            decisions.observe()
    finally:
        unregister(session, callback, None)
        set_queue(session, None)
        release(session)
        release(key)


if __name__ == "__main__":
    main()
