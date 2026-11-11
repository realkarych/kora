import ctypes
import os
import sys
import threading
from typing import cast

from tests.plugins.storage.pressure_models import PressureFailure

BUSY_STATUS = 0xC010
UTF8 = 0x08000100
ITERATE_PARENTS_RECURSIVELY = 3


class Decisions:
    def __init__(self, image: bytes) -> None:
        self.image = image
        self.observing = False
        self.refused = False
        self.output = threading.Lock()

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


class Frameworks:
    def __init__(self) -> None:
        root = "/System/Library/Frameworks/"
        cf = ctypes.CDLL(root + "CoreFoundation.framework/CoreFoundation")
        da = ctypes.CDLL(root + "DiskArbitration.framework/DiskArbitration")
        iokit = ctypes.CDLL(root + "IOKit.framework/IOKit")
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        ref = ctypes.c_void_p
        port = ctypes.c_uint32
        ffi = ctypes.CFUNCTYPE
        self.approval_type = ffi(ref, ref, ref)
        self.release = ffi(None, ref)(("CFRelease", cf))
        self.type_of = ffi(ctypes.c_ulong, ref)(("CFGetTypeID", cf))
        self.data_type = ffi(ctypes.c_ulong)(("CFDataGetTypeID", cf))()
        self.string_type = ffi(ctypes.c_ulong)(("CFStringGetTypeID", cf))()
        self.data_length = ffi(ctypes.c_long, ref)(("CFDataGetLength", cf))
        self.data_bytes = ffi(ref, ref)(("CFDataGetBytePtr", cf))
        self.string_bytes = ffi(
            ctypes.c_ubyte, ref, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
        )(("CFStringGetCString", cf))
        self.create_string = ffi(ref, ref, ctypes.c_char_p, ctypes.c_uint32)(
            ("CFStringCreateWithCString", cf)
        )
        self.create_session = ffi(ref, ref)(("DASessionCreate", da))
        self.set_queue = ffi(None, ref, ref)(("DASessionSetDispatchQueue", da))
        self.register = ffi(None, ref, ref, self.approval_type, ref)(
            ("DARegisterDiskEjectApprovalCallback", da)
        )
        self.unregister = ffi(None, ref, self.approval_type, ref)(
            ("DAUnregisterApprovalCallback", da)
        )
        self.dissent = ffi(ref, ref, ctypes.c_uint32, ref)(("DADissenterCreate", da))
        self.media = ffi(port, ref)(("DADiskCopyIOMedia", da))
        self.search = ffi(ref, port, ctypes.c_char_p, ref, ref, ctypes.c_uint32)(
            ("IORegistryEntrySearchCFProperty", iokit)
        )
        self.release_object = ffi(ctypes.c_int, port)(("IOObjectRelease", iokit))
        self.create_queue = ffi(ref, ctypes.c_char_p, ref)(
            ("dispatch_queue_create", system)
        )

    def image_path(self, disk: int | None, key: int) -> bytes | None:
        media = 0 if disk is None else cast(int, self.media(disk))
        if media == 0:
            return None
        try:
            value = cast(
                int | None,
                self.search(
                    media, b"IOService", key, None, ITERATE_PARENTS_RECURSIVELY
                ),
            )
        finally:
            self.release_object(media)
        if value is None:
            return None
        try:
            return self.decoded(value)
        finally:
            self.release(value)

    def decoded(self, value: int) -> bytes | None:
        kind = cast(int, self.type_of(value))
        if kind == self.data_type:
            length = cast(int, self.data_length(value))
            start = cast(int | None, self.data_bytes(value))
            if start is None or not 0 < length <= 4096:
                return None
            return canonical(ctypes.string_at(start, length).rstrip(b"\x00"))
        if kind == self.string_type:
            buffer = ctypes.create_string_buffer(4097)
            if not self.string_bytes(value, buffer, len(buffer), UTF8):
                return None
            return canonical(buffer.value)
        return None


def canonical(path: bytes) -> bytes:
    return os.fsencode(os.path.realpath(os.fsdecode(path)))


def main() -> None:
    if sys.platform != "darwin":
        raise PressureFailure("native_platform")
    decisions = Decisions(canonical(os.fsencode(sys.argv[1])))
    native = Frameworks()
    key = cast(int | None, native.create_string(None, b"image-path", UTF8))
    session = cast(int | None, native.create_session(None))
    queue = cast(int | None, native.create_queue(b"kora.eject-dissenter", None))
    if key is None or session is None or queue is None:
        raise PressureFailure("native_session_missing")

    def approve(disk: int | None, context: int | None) -> int | None:
        if not decisions.refuses(native.image_path(disk, key)):
            return None
        return cast(int | None, native.dissent(None, BUSY_STATUS, None))

    callback = native.approval_type(approve)
    native.set_queue(session, queue)
    native.register(session, None, callback, None)
    try:
        decisions.emit(b"ready")
        while sys.stdin.buffer.read(1) == b"o":
            decisions.observe()
    finally:
        native.unregister(session, callback, None)
        native.set_queue(session, None)
        native.release(session)
        native.release(key)


if __name__ == "__main__":
    main()
