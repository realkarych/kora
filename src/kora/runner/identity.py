import ctypes
import errno
import os
import sys
import time
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final, Literal, Protocol, cast

from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.time import Clock, SystemClock

STATUS_CAPACITY_BYTES: Final[int] = 4096
OWNER_STATUS_BYTES: Final[int] = 16384
SCAN_NS: Final[int] = 1_000_000_000
PROCESS_SCAN_ENTRIES: Final[int] = 65536
UNKNOWN_READ_PAUSE_SECONDS: Final[float] = 0.001
PROC_PGRP_ONLY: Final[int] = 2
PROC_UID_ONLY: Final[int] = 4
PROC_RUID_ONLY: Final[int] = 5
PROC_PIDTBSDINFO: Final[int] = 3
ARGUMENT_ALIGNMENT: Final[int] = 8
PROC_PIDT_SHORTBSDINFO: Final[int] = 13
PROC_PIDUNIQIDENTIFIERINFO: Final[int] = 17
PROC_FLAG_LP64: Final[int] = 0x10
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
    parent_pid: int | None = None


@dataclass(frozen=True)
class IdentityObservation:
    state: IdentityState
    process: ProcessSnapshot | None = None


@dataclass(frozen=True)
class GroupObservation:
    state: IdentityState
    members: tuple[tuple[int, IdentityObservation], ...] = ()


@dataclass(frozen=True)
class CommandMember:
    process: ProcessSnapshot
    owned: bool


class CommandRead(StrEnum):
    VERIFIED = "verified"
    OPAQUE = "opaque"
    UNSTABLE = "unstable"

    def matches_prelaunch(self, expected: int, current: int | None) -> bool:
        return self is CommandRead.OPAQUE and expected == current


@dataclass(frozen=True)
class CommandCensus:
    members: tuple[CommandMember, ...]
    complete: bool
    ambiguous_group: bool = False


@dataclass(frozen=True)
class CommandPIDInventory:
    listed: frozenset[int]
    original_group: frozenset[int] = frozenset()
    complete: bool = True

    def confirm_empty(self, follow_up: "CommandPIDInventory | None") -> CommandCensus:
        if follow_up is None:
            return CommandCensus((), False)
        return CommandCensus(
            (),
            self.complete and self == follow_up and not self.original_group,
            bool(follow_up.original_group - self.original_group),
        )


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
    parent_pid: int
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
    flags: int
    status: int
    uid: int
    real_uid: int
    saved_uid: int


class TimeValue(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_long), ("nanoseconds", ctypes.c_long)]


class DarwinUniqueInfo(ctypes.Structure):
    _fields_ = [
        ("image_uuid", ctypes.c_ubyte * 16),
        ("unique_id", ctypes.c_uint64),
        ("parent_unique_id", ctypes.c_uint64),
        ("pid_version", ctypes.c_int32),
        ("reserved2", ctypes.c_uint32),
        ("reserved3", ctypes.c_uint64),
        ("reserved4", ctypes.c_uint64),
    ]
    unique_id: int


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


def environment_contains(environment: bytes, marker: bytes) -> bool:
    entry = marker + b"\0"
    return environment.startswith(entry) or b"\0" + entry in environment


def darwin_environment(data: bytes, marker: bytes, *, alignment: int) -> bool | None:
    word = ctypes.sizeof(ctypes.c_int)
    path_end = data.find(b"\0", word)
    if path_end <= word:
        return None
    count = int.from_bytes(data[:word], sys.byteorder, signed=True)
    length = path_end + 1 - word
    start = word + -(-length // alignment) * alignment
    if not 1 <= count <= len(data) - start or any(data[path_end:start]):
        return None
    for _ in range(count):
        end = data.find(b"\0", start)
        if end < 0:
            return None
        start = end + 1
    environment = data[start:]
    if environment and not environment.endswith(b"\0"):
        return None
    return environment_contains(environment, marker)


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
        max_scan_entries: int = PROCESS_SCAN_ENTRIES,
        status_opener: Callable[[int], BinaryIO] = open_status,
        match_reader: Callable[[int, tuple[bytes, ...]], ArgumentMatch] | None = None,
    ) -> None:
        if (
            isinstance(max_scan_entries, bool)
            or not 1 <= max_scan_entries <= PROCESS_SCAN_ENTRIES
        ):
            raise ValueError("native process scan capacity must be between 1 and 65536")
        self._clock = clock if clock is not None else SystemClock()
        self._max_scan_entries = max_scan_entries
        self._open_status = status_opener
        self._match_reader = match_reader
        self._library: ctypes.CDLL | None = None
        self._host: str | None = None
        self._boot: str | None = None
        self._namespace: str | None = None
        self._owner = os.getpid()
        self._arguments: ctypes.Array[ctypes.c_char] | None = None
        self._marker_buffer: ctypes.Array[ctypes.c_char] | None = None
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
            ProcessSnapshot(
                identity, group, session, fields[0] in (b"Z", b"X"), int(fields[1])
            ),
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
                info.parent_pid,
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
        if self._clock.monotonic_ns() >= scan_deadline:
            return None
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

    def command_prelaunch(self, baseline: dict[int, int], deadline: int) -> None:
        if self._library is None or os.getpid() != self._owner:
            return
        listed = self._listed_pids(PROC_UID_ONLY, os.getuid(), deadline)
        for pid in listed or ():
            if self._clock.monotonic_ns() >= deadline:
                break
            unique = self._darwin_unique_id(pid)
            if unique is not None and self._clock.monotonic_ns() < deadline:
                baseline[pid] = unique

    def _darwin_unique_id(self, pid: int) -> int | None:
        library = self._library
        if library is None:
            return None
        info = DarwinUniqueInfo()
        query = cast(ProcPidInfo, library.proc_pidinfo)
        size = ctypes.sizeof(info)
        result = query(pid, PROC_PIDUNIQIDENTIFIERINFO, 0, ctypes.byref(info), size)
        return info.unique_id if result == size and info.unique_id else None

    def command_census(
        self,
        group: int,
        marker: bytes,
        deadline_ns: int,
        *,
        prelaunch: Mapping[int, int] | None = None,
        known_pids: Set[int] = frozenset(),
    ) -> CommandCensus:
        scan_deadline = min(deadline_ns, self._clock.monotonic_ns() + SCAN_NS)
        if os.getpid() != self._owner or None in (
            self._host,
            self._boot,
            self._namespace,
        ):
            return CommandCensus((), False)
        census = self._command_pids(group, scan_deadline)
        if census is None:
            return CommandCensus((), False)
        original = census.original_group
        members: list[CommandMember] = []
        complete = census.complete
        ambiguous_group = False
        pending_group = set(original)
        opaque: dict[int, int] = {}
        for pid in (*original, *(census.listed - original)):
            if self._clock.monotonic_ns() >= scan_deadline:
                return CommandCensus(
                    tuple(members), False, ambiguous_group or bool(pending_group)
                )
            pending_group.discard(pid)
            if pid == self._owner:
                continue
            member, observation = self.command_member(
                pid, group, marker, listed_in_group=pid in original
            )
            verified = observation is CommandRead.VERIFIED
            if member is not None:
                members.append(member)
            elif pid in original and not verified:
                ambiguous_group = (
                    ambiguous_group
                    or self._clock.monotonic_ns() >= scan_deadline
                    or self.read(pid).state is not IdentityState.ABSENT
                )
            elif (
                observation is CommandRead.OPAQUE
                and prelaunch is not None
                and pid not in known_pids
                and self._clock.monotonic_ns() < scan_deadline
            ):
                unique = prelaunch.get(pid)
                if unique is not None and observation.matches_prelaunch(
                    unique, self._darwin_unique_id(pid)
                ):
                    opaque[pid] = unique
                    verified = True
            complete = complete and verified
        if complete and not members:
            complete = False
            if not original:
                confirmation = census.confirm_empty(
                    self._command_pids(group, scan_deadline)
                )
                complete = confirmation.complete and all(
                    self._clock.monotonic_ns() < scan_deadline
                    and self._darwin_unique_id(pid) == unique
                    for pid, unique in opaque.items()
                )
                ambiguous_group = ambiguous_group or confirmation.ambiguous_group
        return CommandCensus(
            tuple(members),
            complete and self._clock.monotonic_ns() < scan_deadline,
            ambiguous_group,
        )

    def _command_pids(self, group: int, deadline_ns: int) -> CommandPIDInventory | None:
        try:
            listed = self._listed_pids(PROC_UID_ONLY, os.getuid(), deadline_ns)
            if listed is None:
                return None
            if sys.platform != "darwin":
                return CommandPIDInventory(frozenset(listed))
            original = self._listed_pids(PROC_PGRP_ONLY, group, deadline_ns)
            if original is None:
                return None
            group_pids = frozenset(original)
            combined = frozenset(listed) | group_pids
            if len(combined) > self._max_scan_entries:
                return CommandPIDInventory(group_pids, group_pids, complete=False)
            return CommandPIDInventory(combined, group_pids)
        except OSError:
            return None

    def command_member(
        self, pid: int, group: int, marker: bytes, *, listed_in_group: bool = False
    ) -> tuple[CommandMember | None, CommandRead]:
        first = self.read(pid)
        if first.state is IdentityState.ABSENT:
            return None, CommandRead.UNSTABLE
        current = first.process
        if current is None or not complete_identity(current.identity):
            return None, CommandRead.OPAQUE
        adopted = sys.platform == "linux" and current.parent_pid == self._owner
        if current.dead:
            return None, CommandRead.UNSTABLE if adopted else CommandRead.VERIFIED
        owned = True if adopted else self._command_marker(pid, marker)
        relevant = owned is True or listed_in_group or current.process_group_id == group
        observed = self.read(pid).process
        if observed != current:
            if (
                observed is not None
                and not observed.dead
                and (
                    observed.process_group_id == group
                    or (sys.platform == "linux" and observed.parent_pid == self._owner)
                )
            ):
                return CommandMember(observed, False), CommandRead.UNSTABLE
            member = CommandMember(current, False) if relevant else None
            return member, CommandRead.UNSTABLE
        member = CommandMember(current, owned is True) if relevant else None
        if owned is CommandRead.UNSTABLE:
            return member, CommandRead.UNSTABLE
        observation = CommandRead.OPAQUE if owned is None else CommandRead.VERIFIED
        return member, observation

    def _command_marker(
        self, pid: int, marker: bytes
    ) -> bool | None | Literal[CommandRead.UNSTABLE]:
        try:
            if sys.platform == "linux":
                return self._linux_marker(pid, marker)
            return self._darwin_marker(pid, marker)
        except PermissionError:
            return False
        except (OSError, ValueError):
            return None

    def _linux_marker(self, pid: int, marker: bytes) -> bool | None:
        directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY)
        try:
            status = read_bounded(directory, "status", OWNER_STATUS_BYTES)
            if len(status) >= OWNER_STATUS_BYTES:
                return None
            if not owned_by(linux_uids(status), os.getuid()):
                return False
            limit = os.sysconf("SC_ARG_MAX") + 1
            environment = read_bounded(directory, "environ", limit)
            if len(environment) >= limit or (
                environment and not environment.endswith(b"\0")
            ):
                return None
            return environment_contains(environment, marker)
        finally:
            os.close(directory)

    def _darwin_marker(
        self, pid: int, marker: bytes
    ) -> bool | None | Literal[CommandRead.UNSTABLE]:
        library = self._library
        if library is None:
            return None
        owner, alignment = self._darwin_owner(library, pid)
        if owner is ArgumentMatch.DIFFERENT:
            return False
        if owner is ArgumentMatch.ABSENT:
            return CommandRead.UNSTABLE
        if owner is not ArgumentMatch.EQUAL or alignment is None:
            return None
        buffer = self._marker_buffer
        if buffer is None:
            capacity = os.sysconf("SC_ARG_MAX") + ctypes.sizeof(ctypes.c_int)
            self._marker_buffer = buffer = ctypes.create_string_buffer(capacity)
        capacity = len(buffer)
        length = ctypes.c_size_t(capacity)
        name = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        call = cast(Sysctl, library.sysctl)
        try:
            result = call(name, 3, buffer, ctypes.byref(length), None, 0)
            failure = ctypes.get_errno() if result != 0 else 0
            if failure == errno.ESRCH or self._darwin_owner(library, pid) != (
                owner,
                alignment,
            ):
                return CommandRead.UNSTABLE
            if result != 0:
                raise OSError(failure)
            if not 0 < length.value < capacity:
                return None
            data = ctypes.string_at(ctypes.addressof(buffer), length.value)
            return darwin_environment(data, marker, alignment=alignment)
        finally:
            ctypes.memset(buffer, 0, capacity)

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
            match = self._settled(pid, expected, scan_deadline)
            if match is ArgumentMatch.UNKNOWN:
                return ProcessScanFailure.OWNER_OR_ARGV_UNKNOWN
            if match is ArgumentMatch.EQUAL:
                found.append(pid)
        return tuple(found)

    def carries(
        self, pid: int, argv: tuple[str, ...], deadline_ns: int | None = None
    ) -> ArgumentMatch:
        if os.getpid() != self._owner:
            return ArgumentMatch.UNKNOWN
        try:
            expected = encoded_arguments(argv)
        except UnicodeError:
            return ArgumentMatch.UNKNOWN
        if deadline_ns is None:
            return self._read_match(pid, expected)
        settle_deadline = min(deadline_ns, self._clock.monotonic_ns() + SCAN_NS)
        return self._settled(pid, expected, settle_deadline)

    def _settled(
        self, pid: int, expected: tuple[bytes, ...], deadline_ns: int
    ) -> ArgumentMatch:
        match = self._read_match(pid, expected)
        while (
            match is ArgumentMatch.UNKNOWN and self._clock.monotonic_ns() < deadline_ns
        ):
            time.sleep(UNKNOWN_READ_PAUSE_SECONDS)
            match = self._read_match(pid, expected)
        return match

    def _read_match(self, pid: int, expected: tuple[bytes, ...]) -> ArgumentMatch:
        if self._match_reader is None:
            return self._carries(pid, expected)
        return self._match_reader(pid, expected)

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

    def _darwin_owner(
        self, library: ctypes.CDLL, pid: int
    ) -> tuple[ArgumentMatch, int | None]:
        info = ShortBsdInfo()
        read_info = cast(ProcPidInfo, library.proc_pidinfo)
        result = read_info(
            pid, PROC_PIDT_SHORTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if result != ctypes.sizeof(info):
            if result == 0 and ctypes.get_errno() == errno.ESRCH:
                return ArgumentMatch.ABSENT, None
            return ArgumentMatch.UNKNOWN, None
        if info.status == DARWIN_ZOMBIE:
            return ArgumentMatch.ABSENT, None
        uids = (info.real_uid, info.uid, info.saved_uid)
        if not owned_by(uids, os.getuid()):
            return ArgumentMatch.DIFFERENT, None
        alignment = ARGUMENT_ALIGNMENT if info.flags & PROC_FLAG_LP64 else 4
        return ArgumentMatch.EQUAL, alignment

    def _darwin_carries(self, pid: int, expected: tuple[bytes, ...]) -> ArgumentMatch:
        library = self._library
        if library is None:
            return ArgumentMatch.UNKNOWN
        owner, _ = self._darwin_owner(library, pid)
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
                        if self._darwin_owner(library, pid)[0] is ArgumentMatch.ABSENT
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
