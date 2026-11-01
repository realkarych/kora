import ctypes
import errno
import os
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, cast

from kora.protocols.identity_records import ProcessIdentity
from kora.runner.identity import IdentityReader, IdentityState, complete_identity

SO_PEERPIDFD: Final[int] = 77
SOL_LOCAL: Final[int] = 0
LOCAL_PEERCRED: Final[int] = 1
LOCAL_PEERPID: Final[int] = 2
XUCRED_BYTES: Final[int] = 76
UCRED: Final[struct.Struct] = struct.Struct("3i")
PID: Final[struct.Struct] = struct.Struct("i")
XUCRED_HEAD: Final[struct.Struct] = struct.Struct("II")
RUSAGE_INFO_V0: Final[int] = 0
RUSAGE_INFO_BYTES: Final[int] = 96
START_ABSTIME_OFFSET: Final[int] = 80
UNSUPPORTED: Final[frozenset[int]] = frozenset({errno.ENOPROTOOPT, errno.EINVAL})
NANOSECONDS_PER_SECOND: Final[int] = 1_000_000_000
PEERCRED: Final[int] = socket.SO_PEERCRED if sys.platform == "linux" else 17
BOOT_CLOCK: Final[int] = time.CLOCK_BOOTTIME if sys.platform == "linux" else 7


class PeerRefusal(StrEnum):
    UID = "uid"
    PID_ZERO = "pid_zero"
    UNREADABLE = "unreadable"
    EXITED = "exited"
    STARTED_AFTER_ACCEPT = "started_after_accept"


@dataclass(frozen=True)
class PeerCredentials:
    uid: int
    pid: int


class PeerRefused(Exception):
    def __init__(self, reason: PeerRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class PeerSource(Protocol):
    def stamp(self) -> int: ...

    def attest(
        self, endpoint: socket.socket, accepted: int | None
    ) -> ProcessIdentity: ...


class ProcPidRusage(Protocol):
    def __call__(self, pid: int, flavor: int, buffer: object) -> int: ...


class MachAbsoluteTime(Protocol):
    def __call__(self) -> int: ...


def peer_option(endpoint: socket.socket, level: int, option: int, size: int) -> bytes:
    try:
        return endpoint.getsockopt(level, option, size)
    except OSError:
        raise PeerRefused(PeerRefusal.UNREADABLE) from None


def checked_peer(uid: int, pid: int) -> None:
    if uid != os.getuid():
        raise PeerRefused(PeerRefusal.UID)
    if pid <= 0:
        raise PeerRefused(PeerRefusal.PID_ZERO)


def live_identity(reader: IdentityReader, pid: int) -> ProcessIdentity:
    observation = reader.read(pid)
    process = observation.process
    if observation.state is IdentityState.ABSENT:
        raise PeerRefused(PeerRefusal.EXITED)
    if (
        observation.state is not IdentityState.PRESENT
        or process is None
        or not complete_identity(process.identity)
    ):
        raise PeerRefused(PeerRefusal.UNREADABLE)
    if process.dead:
        raise PeerRefused(PeerRefusal.EXITED)
    return process.identity


def unchanged(reader: IdentityReader, identity: ProcessIdentity) -> ProcessIdentity:
    if live_identity(reader, identity.pid) != identity:
        raise PeerRefused(PeerRefusal.EXITED)
    return identity


def pidfd_pid(descriptor: int) -> int:
    with open(f"/proc/self/fdinfo/{descriptor}", "rb") as info:
        for line in info:
            if line.startswith(b"Pid:"):
                return int(line.split()[1])
    raise PeerRefused(PeerRefusal.UNREADABLE)


def exited(descriptor: int) -> bool:
    watched = select.poll()
    watched.register(descriptor, select.POLLIN)
    return bool(watched.poll(0))


class PidfdSource:
    def __init__(self, reader: IdentityReader) -> None:
        self._reader = reader

    def stamp(self) -> int:
        return 0

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        credentials = peer_option(endpoint, socket.SOL_SOCKET, PEERCRED, UCRED.size)
        _, uid, _ = UCRED.unpack(credentials)
        raw = peer_option(endpoint, socket.SOL_SOCKET, SO_PEERPIDFD, PID.size)
        descriptor = PID.unpack(raw)[0]
        try:
            try:
                pid = pidfd_pid(descriptor)
            except (OSError, ValueError, IndexError):
                raise PeerRefused(PeerRefusal.UNREADABLE) from None
            if pid < 0:
                raise PeerRefused(PeerRefusal.EXITED)
            checked_peer(uid, pid)
            identity = live_identity(self._reader, pid)
            if exited(descriptor):
                raise PeerRefused(PeerRefusal.EXITED)
            return identity
        finally:
            os.close(descriptor)


class CredentialSource:
    def __init__(self, reader: IdentityReader) -> None:
        self._reader = reader
        self._tick_ns = NANOSECONDS_PER_SECOND // os.sysconf("SC_CLK_TCK")

    def stamp(self) -> int:
        return time.clock_gettime_ns(BOOT_CLOCK)

    def credentials(self, endpoint: socket.socket) -> PeerCredentials:
        raw = peer_option(endpoint, socket.SOL_SOCKET, PEERCRED, UCRED.size)
        pid, uid, _ = UCRED.unpack(raw)
        return PeerCredentials(uid, pid)

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        return self.judge(self.credentials(endpoint), accepted)

    def judge(
        self, credentials: PeerCredentials, accepted: int | None
    ) -> ProcessIdentity:
        checked_peer(credentials.uid, credentials.pid)
        identity = live_identity(self._reader, credentials.pid)
        if (
            accepted is not None
            and (identity.start_time + 1) * self._tick_ns > accepted
        ):
            raise PeerRefused(PeerRefusal.STARTED_AFTER_ACCEPT)
        return unchanged(self._reader, identity)


class DarwinSource:
    def __init__(self, reader: IdentityReader) -> None:
        self._reader = reader
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        library.proc_pid_rusage.restype = ctypes.c_int
        library.mach_absolute_time.argtypes = []
        library.mach_absolute_time.restype = ctypes.c_uint64
        self._rusage = cast(ProcPidRusage, library.proc_pid_rusage)
        self._absolute = cast(MachAbsoluteTime, library.mach_absolute_time)

    def stamp(self) -> int:
        return self._absolute()

    def started(self, pid: int) -> int:
        buffer = ctypes.create_string_buffer(RUSAGE_INFO_BYTES)
        if self._rusage(pid, RUSAGE_INFO_V0, buffer) != 0:
            if ctypes.get_errno() == errno.ESRCH:
                raise PeerRefused(PeerRefusal.EXITED)
            raise PeerRefused(PeerRefusal.UNREADABLE)
        return struct.unpack_from("Q", buffer.raw, START_ABSTIME_OFFSET)[0]

    def credentials(self, endpoint: socket.socket) -> PeerCredentials:
        raw = peer_option(endpoint, SOL_LOCAL, LOCAL_PEERCRED, XUCRED_BYTES)
        version, uid = XUCRED_HEAD.unpack_from(raw)
        if version != 0:
            raise PeerRefused(PeerRefusal.UNREADABLE)
        pid = PID.unpack(peer_option(endpoint, SOL_LOCAL, LOCAL_PEERPID, PID.size))[0]
        return PeerCredentials(uid, pid)

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        return self.judge(self.credentials(endpoint), accepted)

    def judge(
        self, credentials: PeerCredentials, accepted: int | None
    ) -> ProcessIdentity:
        pid = credentials.pid
        checked_peer(credentials.uid, pid)
        identity = live_identity(self._reader, pid)
        if accepted is not None and self.started(pid) > accepted:
            raise PeerRefused(PeerRefusal.STARTED_AFTER_ACCEPT)
        return unchanged(self._reader, identity)


def supports_pidfd() -> bool:
    left, right = socket.socketpair()
    try:
        raw = left.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD, PID.size)
    except OSError as error:
        if error.errno in UNSUPPORTED:
            return False
        raise
    finally:
        left.close()
        right.close()
    os.close(PID.unpack(raw)[0])
    return True


def kernel_source(reader: IdentityReader) -> PeerSource:
    if sys.platform == "darwin":
        return DarwinSource(reader)
    if supports_pidfd():
        return PidfdSource(reader)
    return CredentialSource(reader)
