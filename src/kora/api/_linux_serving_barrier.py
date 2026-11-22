import ctypes
import os
import platform
import sys
from enum import StrEnum
from typing import Protocol, cast

_LIBC_PATH = "/usr/lib/x86_64-linux-gnu/libc.so.6"
_AUDIT_ARCH_X86_64 = 0xC000003E
_X32_SYSCALL_BIT = 0x40000000
_X32_LEGACY_FIRST = 512
_X32_LEGACY_END = 548
_NR_OFFSET = 0
_ARCH_OFFSET = 4
_ARGUMENTS_OFFSET = 16
_X64_CLONE = 56
_X64_FORK = 57
_X64_VFORK = 58
_X64_EXECVE = 59
_X64_UNSHARE = 272
_X64_SECCOMP = 317
_X64_EXECVEAT = 322
_X64_CLONE3 = 435
_X64_CLOSE_RANGE = 436
_GLIBC_PTHREAD_CLONE_FLAGS = 0x003D0F00
_CLONE_FILES = 0x400
_CLOSE_RANGE_UNSHARE = 0x2
_LOAD_WORD = 0x20
_JUMP_EQUAL = 0x15
_JUMP_AT_LEAST = 0x35
_JUMP_BIT_SET = 0x45
_RETURN = 0x06
_ALLOW = 0x7FFF0000
_ERRNO = 0x00050000
_LINUX_EPERM = 1
_LINUX_ENOSYS = 38
_DENY = _ERRNO | _LINUX_EPERM
_NO_SYSCALL = _ERRNO | _LINUX_ENOSYS
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_FILTER_FLAG_TSYNC = 1


class LinuxServingBarrierRefusal(StrEnum):
    UNSUPPORTED_PROFILE = "unsupported_profile"
    NATIVE_ABI_UNAVAILABLE = "native_abi_unavailable"
    NON_DUMPABLE_REFUSED = "non_dumpable_refused"
    NO_NEW_PRIVS_REFUSED = "no_new_privs_refused"
    TSYNC_REFUSED = "tsync_refused"


class LinuxServingBarrierError(RuntimeError):
    def __init__(self, reason: LinuxServingBarrierRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class _SockFilter(ctypes.Structure):
    code: int
    when_true: int
    when_false: int
    value: int

    _fields_ = [
        ("code", ctypes.c_uint16),
        ("when_true", ctypes.c_uint8),
        ("when_false", ctypes.c_uint8),
        ("value", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("instructions", ctypes.c_void_p)]


class _LibcVersion(Protocol):
    def __call__(self) -> bytes: ...


class _Prctl(Protocol):
    def __call__(
        self, operation: int, arg1: int, arg2: int, arg3: int, arg4: int, /
    ) -> int: ...


class _SeccompSyscall(Protocol):
    def __call__(
        self, number: int, mode: int, flags: int, program: ctypes.c_void_p, /
    ) -> int: ...


def _argument_offset(index: int) -> int:
    return _ARGUMENTS_OFFSET + 8 * index


def _load(offset: int) -> _SockFilter:
    return _SockFilter(_LOAD_WORD, 0, 0, offset)


def _equal(value: int, *, yes: int = 0, no: int = 0) -> _SockFilter:
    return _SockFilter(_JUMP_EQUAL, yes, no, value)


def _at_least(value: int, *, yes: int = 0, no: int = 0) -> _SockFilter:
    return _SockFilter(_JUMP_AT_LEAST, yes, no, value)


def _bit_set(mask: int, *, no: int) -> _SockFilter:
    return _SockFilter(_JUMP_BIT_SET, 0, no, mask)


def _ret(value: int) -> _SockFilter:
    return _SockFilter(_RETURN, 0, 0, value)


def _case(number: int, *instructions: _SockFilter) -> tuple[_SockFilter, ...]:
    return (_equal(number, no=len(instructions)), *instructions)


def _deny_flag(number: int, index: int, mask: int) -> tuple[_SockFilter, ...]:
    return _case(
        number,
        _load(_argument_offset(index)),
        _bit_set(mask, no=1),
        _ret(_DENY),
        _ret(_ALLOW),
    )


def linux_serving_filter_instructions() -> ctypes.Array[_SockFilter]:
    instructions = (
        _load(_ARCH_OFFSET),
        _equal(_AUDIT_ARCH_X86_64, yes=1),
        _ret(_DENY),
        _load(_NR_OFFSET),
        _bit_set(_X32_SYSCALL_BIT, no=1),
        _ret(_DENY),
        _at_least(_X32_LEGACY_FIRST, no=2),
        _at_least(_X32_LEGACY_END, yes=1),
        _ret(_DENY),
        *_case(_X64_FORK, _ret(_DENY)),
        *_case(_X64_VFORK, _ret(_DENY)),
        *_case(_X64_EXECVE, _ret(_DENY)),
        *_case(_X64_EXECVEAT, _ret(_DENY)),
        *_case(_X64_CLONE3, _ret(_NO_SYSCALL)),
        *_case(
            _X64_CLONE,
            _load(_argument_offset(0) + 4),
            _equal(0, yes=1),
            _ret(_DENY),
            _load(_argument_offset(0)),
            _equal(_GLIBC_PTHREAD_CLONE_FLAGS, yes=1),
            _ret(_DENY),
            _ret(_ALLOW),
        ),
        *_deny_flag(_X64_UNSHARE, 0, _CLONE_FILES),
        *_deny_flag(_X64_CLOSE_RANGE, 2, _CLOSE_RANGE_UNSHARE),
        _ret(_ALLOW),
    )
    return (_SockFilter * len(instructions))(*instructions)


def _require_candidate_profile() -> None:
    if (
        sys.platform != "linux"
        or sys.implementation.name != "cpython"
        or sys.version_info != (3, 11, 17, "final", 0)
        or os.uname().machine != "x86_64"
        or os.geteuid() == 0
    ):
        raise LinuxServingBarrierError(LinuxServingBarrierRefusal.UNSUPPORTED_PROFILE)
    try:
        os_release = platform.freedesktop_os_release()
    except OSError:
        raise LinuxServingBarrierError(
            LinuxServingBarrierRefusal.UNSUPPORTED_PROFILE
        ) from None
    if (os_release.get("ID"), os_release.get("VERSION_ID")) != ("ubuntu", "24.04"):
        raise LinuxServingBarrierError(LinuxServingBarrierRefusal.UNSUPPORTED_PROFILE)


def _require_native_layout() -> None:
    if (
        sys.byteorder != "little"
        or ctypes.sizeof(ctypes.c_long) != 8
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(_SockFilter) != 8
        or ctypes.sizeof(_SockFprog) != 16
    ):
        raise LinuxServingBarrierError(
            LinuxServingBarrierRefusal.NATIVE_ABI_UNAVAILABLE
        )


def install_linux_serving_barrier() -> None:
    _require_candidate_profile()
    _require_native_layout()
    try:
        libc = ctypes.CDLL(_LIBC_PATH)
        version_call = libc.gnu_get_libc_version
        prctl_call = libc.prctl
        syscall = libc.syscall
    except (OSError, AttributeError):
        raise LinuxServingBarrierError(
            LinuxServingBarrierRefusal.NATIVE_ABI_UNAVAILABLE
        ) from None

    version_call.argtypes = []
    version_call.restype = ctypes.c_char_p
    if cast(_LibcVersion, version_call)() != b"2.39":
        raise LinuxServingBarrierError(LinuxServingBarrierRefusal.UNSUPPORTED_PROFILE)

    prctl_call.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl_call.restype = ctypes.c_int
    syscall.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    syscall.restype = ctypes.c_long
    prctl = cast(_Prctl, prctl_call)
    seccomp = cast(_SeccompSyscall, syscall)
    instructions = linux_serving_filter_instructions()
    program = _SockFprog(len(instructions), ctypes.addressof(instructions))

    if prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise LinuxServingBarrierError(LinuxServingBarrierRefusal.NON_DUMPABLE_REFUSED)
    if prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise LinuxServingBarrierError(LinuxServingBarrierRefusal.NO_NEW_PRIVS_REFUSED)
    if (
        seccomp(
            _X64_SECCOMP,
            _SECCOMP_SET_MODE_FILTER,
            _SECCOMP_FILTER_FLAG_TSYNC,
            ctypes.c_void_p(ctypes.addressof(program)),
        )
        != 0
    ):
        raise LinuxServingBarrierError(LinuxServingBarrierRefusal.TSYNC_REFUSED)
