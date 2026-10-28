import ctypes
import errno
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from kora.protocols.identity import ProcessIdentity
from kora.protocols.time import Clock, SystemClock


def complete_identity(identity: ProcessIdentity) -> bool:
    return all(
        value is not None
        for value in (identity.host_id, identity.boot_id, identity.pid_namespace)
    )


class IdentityState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessSnapshot:
    identity: ProcessIdentity
    process_group_id: int
    session_id: int
    dead: bool


@dataclass(frozen=True)
class IdentityObservation:
    state: IdentityState
    process: ProcessSnapshot | None = None


@dataclass(frozen=True)
class GroupObservation:
    state: IdentityState
    members: tuple[tuple[int, IdentityObservation], ...] = ()


class IdentityReader(Protocol):
    def read(self, pid: int) -> IdentityObservation: ...

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation: ...


class BsdInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("exit_status", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("parent_pid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("real_uid", ctypes.c_uint32),
        ("real_gid", ctypes.c_uint32),
        ("saved_uid", ctypes.c_uint32),
        ("saved_gid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("command", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("files", ctypes.c_uint32),
        ("process_group_id", ctypes.c_uint32),
        ("job_control", ctypes.c_uint32),
        ("terminal", ctypes.c_uint32),
        ("terminal_group", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    ]
    status: int
    pid: int
    process_group_id: int
    start_seconds: int
    start_microseconds: int


class TimeValue(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_long), ("nanoseconds", ctypes.c_long)]


class ProcPidInfo(Protocol):
    def __call__(
        self, pid: int, flavor: int, argument: int, buffer: object, size: int
    ) -> int: ...


class ProcListPids(Protocol):
    def __call__(self, kind: int, group: int, buffer: object, size: int) -> int: ...


class SysctlByName(Protocol):
    def __call__(
        self, name: bytes, old: object, length: object, new: None, size: int
    ) -> int: ...


class GetHostUuid(Protocol):
    def __call__(self, result: object, timeout: object) -> int: ...


class NativeIdentityReader:
    def __init__(
        self, *, clock: Clock | None = None, max_scan_entries: int = 65536
    ) -> None:
        if isinstance(max_scan_entries, bool) or not 1 <= max_scan_entries <= 65536:
            raise ValueError("native process scan capacity must be between 1 and 65536")
        self._clock = clock if clock is not None else SystemClock()
        self._max_scan_entries = max_scan_entries
        self._library: ctypes.CDLL | None = None
        self._host: str | None = None
        self._boot: str | None = None
        if sys.platform == "darwin":
            self._library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            library = self._library
            library.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            library.proc_pidinfo.restype = ctypes.c_int
            library.proc_listpids.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            library.proc_listpids.restype = ctypes.c_int
            library.sysctlbyname.argtypes = [
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            library.sysctlbyname.restype = ctypes.c_int
            library.gethostuuid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            library.gethostuuid.restype = ctypes.c_int
            host = (ctypes.c_ubyte * 16)()
            timeout = TimeValue(0, 0)
            read_host = cast(GetHostUuid, library.gethostuuid)
            if read_host(host, ctypes.byref(timeout)) == 0:
                self._host = bytes(host).hex()
            boot = ctypes.create_string_buffer(256)
            length = ctypes.c_size_t(len(boot))
            read_boot = cast(SysctlByName, library.sysctlbyname)
            if (
                read_boot(b"kern.bootsessionuuid", boot, ctypes.byref(length), None, 0)
                == 0
            ):
                self._boot = boot.value.decode("ascii")
        elif sys.platform == "linux":
            try:
                self._host = Path("/etc/machine-id").read_text().strip()
                self._boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            except OSError:
                self._host = None
                self._boot = None
        else:
            raise ValueError("unsupported local process platform")

    def read(self, pid: int) -> IdentityObservation:
        try:
            if sys.platform == "linux":
                return self._linux_read(pid)
            return self._darwin_read(pid)
        except (ProcessLookupError, FileNotFoundError):
            return IdentityObservation(IdentityState.ABSENT)
        except (OSError, UnicodeError, ValueError, IndexError):
            return IdentityObservation(IdentityState.UNKNOWN)

    def _linux_read(self, pid: int) -> IdentityObservation:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        namespace = os.readlink(f"/proc/{pid}/ns/pid")
        group = int(fields[2])
        session = int(fields[3])
        identity = ProcessIdentity(
            host_id=self._host,
            boot_id=self._boot,
            pid_namespace=namespace,
            pid=pid,
            start_time=int(fields[19]),
            leads_group=pid == group,
        )
        return IdentityObservation(
            IdentityState.PRESENT,
            ProcessSnapshot(identity, group, session, fields[0] in ("Z", "X")),
        )

    def _darwin_read(self, pid: int) -> IdentityObservation:
        library = self._library
        if library is None:
            return IdentityObservation(IdentityState.UNKNOWN)
        info = BsdInfo()
        read_info = cast(ProcPidInfo, library.proc_pidinfo)
        result = read_info(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if result != ctypes.sizeof(info):
            if result == 0 and ctypes.get_errno() == errno.ESRCH:
                return IdentityObservation(IdentityState.ABSENT)
            return IdentityObservation(IdentityState.UNKNOWN)
        identity = ProcessIdentity(
            host_id=self._host,
            boot_id=self._boot,
            pid_namespace=self._boot,
            pid=info.pid,
            start_time=info.start_seconds * 1_000_000 + info.start_microseconds,
            leads_group=info.pid == info.process_group_id,
        )
        return IdentityObservation(
            IdentityState.PRESENT,
            ProcessSnapshot(
                identity, info.process_group_id, os.getsid(pid), info.status == 5
            ),
        )

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        scan_deadline = self._clock.monotonic_ns() + 1_000_000_000
        if deadline_ns is not None:
            scan_deadline = min(scan_deadline, deadline_ns)
        if self._clock.monotonic_ns() >= scan_deadline:
            return GroupObservation(IdentityState.UNKNOWN)
        if sys.platform == "linux":
            members: list[tuple[int, IdentityObservation]] = []
            try:
                entries = os.scandir("/proc")
                with entries:
                    for index, entry in enumerate(entries):
                        if (
                            index >= self._max_scan_entries
                            or self._clock.monotonic_ns() >= scan_deadline
                        ):
                            return GroupObservation(IdentityState.UNKNOWN)
                        if not entry.name.isdecimal():
                            continue
                        pid = int(entry.name)
                        try:
                            fields = (
                                Path(f"/proc/{pid}/stat")
                                .read_text()
                                .rsplit(")", 1)[1]
                                .split()
                            )
                        except FileNotFoundError:
                            continue
                        if int(fields[2]) != process_group_id:
                            continue
                        observation = self.read(pid)
                        if observation.state is IdentityState.UNKNOWN:
                            return GroupObservation(IdentityState.UNKNOWN)
                        process = observation.process
                        if (
                            process is None
                            or process.process_group_id == process_group_id
                        ):
                            members.append((pid, observation))
            except (OSError, ValueError, IndexError, UnicodeError):
                return GroupObservation(IdentityState.UNKNOWN)
            if self._clock.monotonic_ns() >= scan_deadline:
                return GroupObservation(IdentityState.UNKNOWN)
            return GroupObservation(IdentityState.PRESENT, tuple(members))
        library = self._library
        if library is None:
            return GroupObservation(IdentityState.UNKNOWN)
        buffer = (ctypes.c_int * self._max_scan_entries)()
        list_pids = cast(ProcListPids, library.proc_listpids)
        ctypes.set_errno(0)
        size = list_pids(2, process_group_id, buffer, ctypes.sizeof(buffer))
        failure = ctypes.get_errno()
        if (
            size < 0
            or failure != 0
            or size >= ctypes.sizeof(buffer)
            or size % ctypes.sizeof(ctypes.c_int)
            or self._clock.monotonic_ns() >= scan_deadline
        ):
            return GroupObservation(IdentityState.UNKNOWN)
        darwin_members: list[tuple[int, IdentityObservation]] = []
        for index in range(size // ctypes.sizeof(ctypes.c_int)):
            if self._clock.monotonic_ns() >= scan_deadline:
                return GroupObservation(IdentityState.UNKNOWN)
            pid = buffer[index]
            if pid > 0:
                darwin_members.append((pid, self.read(pid)))
        if self._clock.monotonic_ns() >= scan_deadline:
            return GroupObservation(IdentityState.UNKNOWN)
        return GroupObservation(IdentityState.PRESENT, tuple(darwin_members))
