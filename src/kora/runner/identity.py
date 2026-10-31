import ctypes
import errno
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final, Protocol, cast

from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.time import Clock, SystemClock

STATUS_CAPACITY_BYTES: Final[int] = 4096
OWNER_STATUS_BYTES: Final[int] = 16384
SCAN_NS: Final[int] = 1_000_000_000
PROC_PGRP_ONLY: Final[int] = 2
PROC_RUID_ONLY: Final[int] = 5
PROC_PIDTBSDINFO: Final[int] = 3
ARGUMENT_ALIGNMENT: Final[int] = 8
PROC_PIDT_SHORTBSDINFO: Final[int] = 13
DARWIN_ZOMBIE: Final[int] = 5
CTL_KERN: Final[int] = 1
KERN_ARGMAX: Final[int] = 8
KERN_PROCARGS2: Final[int] = 49


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


class ArgumentMatch(StrEnum):
    EQUAL = "equal"
    DIFFERENT = "different"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class ProcessScanFailure(StrEnum):
    READER_PROCESS_CHANGED = "reader_process_changed"
    PID_LISTING_INCOMPLETE = "pid_listing_incomplete"
    SCAN_DEADLINE = "scan_deadline"
    OWNER_OR_ARGV_UNKNOWN = "owner_or_argv_unknown"


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


class ShortBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint32),
        ("parent_pid", ctypes.c_uint32),
        ("process_group_id", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("command", ctypes.c_char * 16),
        ("flags", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("real_uid", ctypes.c_uint32),
        ("real_gid", ctypes.c_uint32),
        ("saved_uid", ctypes.c_uint32),
        ("saved_gid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]
    status: int
    uid: int
    real_uid: int
    saved_uid: int


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


class Sysctl(Protocol):
    def __call__(
        self,
        name: object,
        length: int,
        old: object,
        size: object,
        new: None,
        new_size: int,
    ) -> int: ...


class GetHostUuid(Protocol):
    def __call__(self, result: object, timeout: object) -> int: ...


VISIBLE_PROCFS_OPTIONS: frozenset[str] = frozenset({"hidepid=0", "hidepid=off"})


def hides_processes(option: str) -> bool:
    if option.startswith("hidepid="):
        return option not in VISIBLE_PROCFS_OPTIONS
    return option == "subset=pid"


def mount_hides_processes(mountinfo: bytes) -> bool:
    if len(mountinfo) > 1048576:
        return True
    for line in mountinfo.decode("utf-8", "surrogateescape").splitlines():
        fields, separator, rest = line.partition(" - ")
        mount = fields.split(" ")
        described = rest.split(" ")
        if separator and len(mount) >= 5 and mount[4] == "/proc":
            if len(described) < 3 or described[0] != "proc":
                return True
            if any(hides_processes(option) for option in described[2].split(",")):
                return True
    return False


def procfs_hides_processes() -> bool:
    with open("/proc/self/mountinfo", "rb") as source:
        return mount_hides_processes(source.read(1048577))


def procfs_is_own() -> bool:
    status = Path("/proc/self/status").read_text()
    lines = [line for line in status.splitlines() if line.startswith("NSpid:")]
    return len(lines) == 1 and lines[0].split() == ["NSpid:", str(os.getpid())]


def observer_namespace() -> str | None:
    try:
        if not procfs_is_own() or procfs_hides_processes():
            return None
        return os.readlink("/proc/self/ns/pid")
    except (OSError, UnicodeError):
        return None


def encoded_arguments(argv: tuple[str, ...]) -> tuple[bytes, ...]:
    return tuple(os.fsencode(argument) for argument in argv)


def read_bounded(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        chunks: list[bytes] = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def owned_by(uids: tuple[int, ...], uid: int) -> bool:
    return len(uids) == 3 and all(value == uid for value in uids)


def linux_uids(status: bytes) -> tuple[int, ...]:
    for line in status.splitlines():
        if line.startswith(b"Uid:"):
            return tuple(int(value) for value in line.split()[1:4])
    raise ValueError("process status has no uids")


def darwin_arguments(
    buffer: ctypes.Array[ctypes.c_char], length: int, expected: tuple[bytes, ...]
) -> ArgumentMatch:
    if length < 4:
        return ArgumentMatch.UNKNOWN
    if int.from_bytes(ctypes.string_at(buffer, 4), sys.byteorder) != len(expected):
        return ArgumentMatch.DIFFERENT
    address = ctypes.addressof(buffer)
    path = ctypes.string_at(address + 4)
    path_end = 4 + len(path)
    start = 4 + -(-(len(path) + 1) // ARGUMENT_ALIGNMENT) * ARGUMENT_ALIGNMENT
    if start > length or ctypes.string_at(address + path_end, start - path_end).strip(
        b"\0"
    ):
        return ArgumentMatch.UNKNOWN
    wanted = b"".join(argument + b"\0" for argument in expected)
    window = ctypes.string_at(address + start, min(length - start, len(wanted)))
    return ArgumentMatch.EQUAL if window == wanted else ArgumentMatch.DIFFERENT


def open_status(pid: int) -> BinaryIO:
    return open(f"/proc/{pid}/stat", "rb", buffering=0)


def status_fields(status: BinaryIO) -> tuple[bytes, ...]:
    content = status.read(STATUS_CAPACITY_BYTES)
    _, separator, tail = content.rpartition(b")")
    if not separator or len(content) >= STATUS_CAPACITY_BYTES:
        raise ValueError("process status is malformed")
    return tuple(tail.split())


class NativeIdentityReader:
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        max_scan_entries: int = 65536,
        status_opener: Callable[[int], BinaryIO] = open_status,
    ) -> None:
        if isinstance(max_scan_entries, bool) or not 1 <= max_scan_entries <= 65536:
            raise ValueError("native process scan capacity must be between 1 and 65536")
        self._clock = clock if clock is not None else SystemClock()
        self._max_scan_entries = max_scan_entries
        self._open_status = status_opener
        self._library: ctypes.CDLL | None = None
        self._host: str | None = None
        self._boot: str | None = None
        self._namespace: str | None = None
        self._owner = os.getpid()
        self._arguments: ctypes.Array[ctypes.c_char] | None = None
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
            self._namespace = self._boot
        elif sys.platform == "linux":
            try:
                self._host = Path("/etc/machine-id").read_text().strip()
                self._boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            except OSError:
                self._host = None
                self._boot = None
            self._namespace = observer_namespace()
        else:
            raise ValueError("unsupported local process platform")

    def read(self, pid: int) -> IdentityObservation:
        if os.getpid() != self._owner:
            return IdentityObservation(IdentityState.UNKNOWN)
        try:
            if sys.platform == "linux":
                return self._linux_read(pid)
            return self._darwin_read(pid)
        except (ProcessLookupError, FileNotFoundError):
            return IdentityObservation(IdentityState.ABSENT)
        except (OSError, UnicodeError, ValueError, IndexError):
            return IdentityObservation(IdentityState.UNKNOWN)

    def _linux_read(self, pid: int) -> IdentityObservation:
        with self._open_status(pid) as status:
            fields = status_fields(status)
        group = int(fields[2])
        session = int(fields[3])
        identity = ProcessIdentity(
            host_id=self._host,
            boot_id=self._boot,
            pid_namespace=self._namespace,
            pid=pid,
            start_time=int(fields[19]),
            leads_group=pid == group,
        )
        return IdentityObservation(
            IdentityState.PRESENT,
            ProcessSnapshot(identity, group, session, fields[0] in (b"Z", b"X")),
        )

    def _darwin_read(self, pid: int) -> IdentityObservation:
        library = self._library
        if library is None:
            return IdentityObservation(IdentityState.UNKNOWN)
        info = BsdInfo()
        read_info = cast(ProcPidInfo, library.proc_pidinfo)
        result = read_info(
            pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if result != ctypes.sizeof(info):
            if result == 0 and ctypes.get_errno() == errno.ESRCH:
                return IdentityObservation(IdentityState.ABSENT)
            return IdentityObservation(IdentityState.UNKNOWN)
        identity = ProcessIdentity(
            host_id=self._host,
            boot_id=self._boot,
            pid_namespace=self._namespace,
            pid=info.pid,
            start_time=info.start_seconds * 1_000_000 + info.start_microseconds,
            leads_group=info.pid == info.process_group_id,
        )
        return IdentityObservation(
            IdentityState.PRESENT,
            ProcessSnapshot(
                identity,
                info.process_group_id,
                os.getsid(pid),
                info.status == DARWIN_ZOMBIE,
            ),
        )

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        scan_deadline = self._clock.monotonic_ns() + SCAN_NS
        if deadline_ns is not None:
            scan_deadline = min(scan_deadline, deadline_ns)
        if self._clock.monotonic_ns() >= scan_deadline:
            return GroupObservation(IdentityState.UNKNOWN)
        if sys.platform == "linux":
            members: list[tuple[int, IdentityObservation]] = []
            try:
                pids = self._listed_pids(
                    PROC_PGRP_ONLY, process_group_id, scan_deadline
                )
                if pids is None:
                    return GroupObservation(IdentityState.UNKNOWN)
                for pid in pids:
                    if self._clock.monotonic_ns() >= scan_deadline:
                        return GroupObservation(IdentityState.UNKNOWN)
                    try:
                        with self._open_status(pid) as status:
                            fields = status_fields(status)
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if int(fields[2]) != process_group_id:
                        continue
                    observation = self.read(pid)
                    if observation.state is IdentityState.UNKNOWN:
                        return GroupObservation(IdentityState.UNKNOWN)
                    process = observation.process
                    if process is None or process.process_group_id == process_group_id:
                        members.append((pid, observation))
            except (OSError, ValueError, IndexError, UnicodeError):
                return GroupObservation(IdentityState.UNKNOWN)
            if self._clock.monotonic_ns() >= scan_deadline:
                return GroupObservation(IdentityState.UNKNOWN)
            return GroupObservation(IdentityState.PRESENT, tuple(members))
        listed = self._listed_pids(PROC_PGRP_ONLY, process_group_id, scan_deadline)
        if listed is None:
            return GroupObservation(IdentityState.UNKNOWN)
        darwin_members: list[tuple[int, IdentityObservation]] = []
        for pid in listed:
            if self._clock.monotonic_ns() >= scan_deadline:
                return GroupObservation(IdentityState.UNKNOWN)
            darwin_members.append((pid, self.read(pid)))
        if self._clock.monotonic_ns() >= scan_deadline:
            return GroupObservation(IdentityState.UNKNOWN)
        return GroupObservation(IdentityState.PRESENT, tuple(darwin_members))

    def _listed_pids(
        self, kind: int, value: int, scan_deadline: int
    ) -> tuple[int, ...] | None:
        if sys.platform == "linux":
            pids: list[int] = []
            with os.scandir("/proc") as entries:
                for index, entry in enumerate(entries):
                    if (
                        index >= self._max_scan_entries
                        or self._clock.monotonic_ns() >= scan_deadline
                    ):
                        return None
                    if entry.name.isdecimal():
                        pids.append(int(entry.name))
            return tuple(pids)
        library = self._library
        if library is None:
            return None
        buffer = (ctypes.c_int * self._max_scan_entries)()
        list_pids = cast(ProcListPids, library.proc_listpids)
        ctypes.set_errno(0)
        size = list_pids(kind, value, buffer, ctypes.sizeof(buffer))
        failure = ctypes.get_errno()
        if (
            size < 0
            or failure != 0
            or size >= ctypes.sizeof(buffer)
            or size % ctypes.sizeof(ctypes.c_int)
            or self._clock.monotonic_ns() >= scan_deadline
        ):
            return None
        count = size // ctypes.sizeof(ctypes.c_int)
        return tuple(pid for pid in buffer[:count] if pid > 0)

    def processes_carrying(
        self, argv: tuple[str, ...], deadline_ns: int
    ) -> tuple[int, ...] | None:
        result = self.scan_processes_carrying(argv, deadline_ns)
        return None if isinstance(result, ProcessScanFailure) else result

    def scan_processes_carrying(
        self, argv: tuple[str, ...], deadline_ns: int
    ) -> tuple[int, ...] | ProcessScanFailure:
        scan_deadline = min(deadline_ns, self._clock.monotonic_ns() + SCAN_NS)
        if os.getpid() != self._owner:
            return ProcessScanFailure.READER_PROCESS_CHANGED
        try:
            listed = self._listed_pids(PROC_RUID_ONLY, os.getuid(), scan_deadline)
        except OSError:
            return ProcessScanFailure.PID_LISTING_INCOMPLETE
        if listed is None:
            return ProcessScanFailure.PID_LISTING_INCOMPLETE
        expected = encoded_arguments(argv)
        found: list[int] = []
        for pid in listed:
            if self._clock.monotonic_ns() >= scan_deadline:
                return ProcessScanFailure.SCAN_DEADLINE
            if pid == self._owner:
                continue
            match = self._carries(pid, expected)
            if match is ArgumentMatch.UNKNOWN:
                return ProcessScanFailure.OWNER_OR_ARGV_UNKNOWN
            if match is ArgumentMatch.EQUAL:
                found.append(pid)
        return tuple(found)

    def carries(self, pid: int, argv: tuple[str, ...]) -> ArgumentMatch:
        if os.getpid() != self._owner:
            return ArgumentMatch.UNKNOWN
        try:
            expected = encoded_arguments(argv)
        except UnicodeError:
            return ArgumentMatch.UNKNOWN
        return self._carries(pid, expected)

    def _carries(self, pid: int, expected: tuple[bytes, ...]) -> ArgumentMatch:
        try:
            if sys.platform == "linux":
                return self._linux_carries(pid, expected)
            return self._darwin_carries(pid, expected)
        except (ProcessLookupError, FileNotFoundError):
            return ArgumentMatch.ABSENT
        except (OSError, ValueError, IndexError):
            return ArgumentMatch.UNKNOWN

    def _linux_carries(self, pid: int, expected: tuple[bytes, ...]) -> ArgumentMatch:
        directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY)
        try:
            status = read_bounded(directory, "status", OWNER_STATUS_BYTES)
            if len(status) >= OWNER_STATUS_BYTES:
                raise ValueError("process status is too long")
            if not owned_by(linux_uids(status), os.getuid()):
                return ArgumentMatch.DIFFERENT
            wanted = b"".join(argument + b"\0" for argument in expected)
            arguments = read_bounded(directory, "cmdline", len(wanted) + 1)
        finally:
            os.close(directory)
        return ArgumentMatch.EQUAL if arguments == wanted else ArgumentMatch.DIFFERENT

    def _darwin_owner(self, library: ctypes.CDLL, pid: int) -> ArgumentMatch:
        info = ShortBsdInfo()
        read_info = cast(ProcPidInfo, library.proc_pidinfo)
        result = read_info(
            pid, PROC_PIDT_SHORTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if result != ctypes.sizeof(info):
            if result == 0 and ctypes.get_errno() == errno.ESRCH:
                return ArgumentMatch.ABSENT
            return ArgumentMatch.UNKNOWN
        if info.status == DARWIN_ZOMBIE:
            return ArgumentMatch.ABSENT
        uids = (info.real_uid, info.uid, info.saved_uid)
        return (
            ArgumentMatch.EQUAL
            if owned_by(uids, os.getuid())
            else ArgumentMatch.DIFFERENT
        )

    def _darwin_carries(self, pid: int, expected: tuple[bytes, ...]) -> ArgumentMatch:
        library = self._library
        if library is None:
            return ArgumentMatch.UNKNOWN
        owner = self._darwin_owner(library, pid)
        if owner is not ArgumentMatch.EQUAL:
            return owner
        buffer = self._argument_buffer(library)
        length = ctypes.c_size_t(len(buffer) - 1)
        try:
            name = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
            call = cast(Sysctl, library.sysctl)
            if call(name, 3, buffer, ctypes.byref(length), None, 0) != 0:
                failure = ctypes.get_errno()
                if failure == errno.ESRCH:
                    return ArgumentMatch.ABSENT
                if failure == errno.EINVAL:
                    return (
                        ArgumentMatch.ABSENT
                        if self._darwin_owner(library, pid) is ArgumentMatch.ABSENT
                        else ArgumentMatch.UNKNOWN
                    )
                return ArgumentMatch.UNKNOWN
            return darwin_arguments(buffer, length.value, expected)
        finally:
            ctypes.memset(buffer, 0, min(length.value, len(buffer)))

    def _argument_buffer(self, library: ctypes.CDLL) -> ctypes.Array[ctypes.c_char]:
        if self._arguments is None:
            maximum = ctypes.c_int(0)
            length = ctypes.c_size_t(ctypes.sizeof(maximum))
            name = (ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX)
            call = cast(Sysctl, library.sysctl)
            if call(name, 2, ctypes.byref(maximum), ctypes.byref(length), None, 0) != 0:
                failure = ctypes.get_errno()
                raise OSError(failure, os.strerror(failure))
            self._arguments = ctypes.create_string_buffer(maximum.value + 1)
        return self._arguments
