import ctypes
import errno
import os
import re
import stat
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

FileIdentity: TypeAlias = tuple[int, int, int, int]
ProcessIdentity: TypeAlias = tuple[int, int, int]
PREFIXES = (
    "diskarbitrationd",
    "diskimages",
    "fsck_hfs",
    "hfs.util",
    "fseventsd",
    "mds",
    "mdworker",
    "revisiond",
    "storagekitd",
    "syspolicyd",
    "XProtect",
    "Xprotect",
)
ENTRY_NAMES = (
    ".Spotlight-V100",
    ".fseventsd",
    ".Trashes",
    ".TemporaryItems",
    ".metadata_never_index",
    ".DocumentRevisions-V100",
)
SYSTEM_ROOTS = (
    "/System/Library/",
    "/System/Applications/",
    "/System/Cryptexes/",
    "/System/iOSSupport/",
    "/usr/libexec/",
    "/usr/sbin/",
    "/usr/bin/",
    "/sbin/",
    "/bin/",
    "/Library/Apple/",
)


@dataclass(frozen=True)
class FileStamp:
    st_mode: int
    st_dev: int
    st_ino: int
    st_rdev: int
    st_uid: int


FileState: TypeAlias = FileStamp | str


@dataclass(frozen=True)
class HolderLimits:
    processes: int = 8192
    holders: int = 8
    descriptors: int = 4096
    references: int = 6
    descriptor_check: int = 256
    note: int = 6144
    seconds: float = 5.0


@dataclass(frozen=True)
class BoundTarget:
    name: str
    path: Path
    identity: FileIdentity | None
    capture: str = "bound"


@dataclass(frozen=True)
class Binding:
    targets: tuple[BoundTarget, ...] = ()
    mountpoint: Path | None = None
    volume_device: int | None = None
    root_identity: tuple[int, int] | None = None
    owner_device: int = 0
    euid: int = 0
    capture: str = "absent"
    entities_truncated: bool = False


@dataclass(frozen=True)
class ObservationContext:
    pid: int
    detach_unique: int | None
    executable: str


@dataclass(frozen=True)
class Holder:
    pid: int
    identity: str
    unique: int = 0
    version: int = 0
    uid: int = 0
    comm: bytes = b""
    executable: str = "other"
    role: str = "other"
    after_detach: str = "unknown"
    references: tuple[str, ...] = ()
    reference_count: int = 0
    errors: tuple[str, ...] = ()
    start: tuple[int, int] = (0, 0)


@dataclass
class Observation:
    capture: str = "absent"
    before: str = "absent"
    after: str = "absent"
    binding: str = "unchanged"
    matching: bool = False
    processes: int = 0
    visited: int = 0
    same_uid: int = 0
    other_uid: int = 0
    holder_count: int = 0
    detach_unique: int | None = None
    query_failure: str = ""
    holders: list[Holder] = field(default_factory=list[Holder])
    candidates: dict[str, tuple[int, int, int, int]] = field(
        default_factory=dict[str, tuple[int, int, int, int]]
    )
    targets: tuple[tuple[str, str, str], ...] = ()
    errors: dict[str, int] = field(default_factory=dict[str, int])
    layouts: set[str] = field(default_factory=set[str])
    entries: str = "unbound"
    truncated: set[str] = field(default_factory=set[str])
    timings: dict[str, int] = field(default_factory=dict[str, int])


def number(data: bytes, format_string: str, offset: int) -> int:
    return int(struct.unpack_from(format_string, data, offset)[0])


def error_name(code: int) -> str:
    return errno.errorcode.get(code, "UNKNOWN")


def inspect_path(path: Path) -> FileState:
    try:
        info = os.lstat(path)
        return FileStamp(
            info.st_mode, info.st_dev, info.st_ino, info.st_rdev, info.st_uid
        )
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "denied"
    except OSError as error:
        return "error." + error_name(error.errno or 0)


def file_identity(information: FileStamp) -> FileIdentity:
    return (
        stat.S_IFMT(information.st_mode),
        information.st_dev & 0xFFFFFFFF,
        information.st_ino,
        information.st_rdev,
    )


def classify_volume(
    observed: FileState,
    volume_device: int | None,
    root_identity: tuple[int, int] | None,
    owner_device: int,
    euid: int,
) -> str:
    if isinstance(observed, str):
        return observed
    if not stat.S_ISDIR(observed.st_mode):
        return "changed"
    device = observed.st_dev & 0xFFFFFFFF
    if device == volume_device:
        return (
            "mounted"
            if root_identity in (None, (device, observed.st_ino))
            else "changed"
        )
    if device == owner_device & 0xFFFFFFFF:
        return (
            "unmounted"
            if observed.st_uid == euid and stat.S_IMODE(observed.st_mode) == 0o700
            else "changed"
        )
    return "foreign_mount"


def binding_changed(
    capture: str,
    before: str,
    after: str,
    nodes: tuple[tuple[str, str], ...],
) -> bool:
    return (
        any(first != "bound" or last != "bound" for first, last in nodes)
        or capture not in {"mounted", "unmounted"}
        or before not in {"mounted", "unmounted"}
        or before != after
        or (capture == "unmounted" and before == "mounted")
    )


def volume_matching(capture: str, before: str, after: str) -> bool:
    return capture == before == after == "mounted"


def capture_binding(
    mountpoint: Path,
    volume: str | None,
    entities: tuple[str, ...],
    owner_device: int,
) -> Binding:
    targets: list[BoundTarget] = []
    root = next(
        (item for item in entities if re.fullmatch(r"/dev/disk[0-9]{1,8}", item)), ""
    )
    selected = 0
    volume_device: int | None = None
    for entity in entities:
        if (
            not root
            or re.fullmatch(re.escape(root) + r"(?:s[0-9]{1,4}){0,2}", entity) is None
        ):
            continue
        selected += 1
        if selected > 8:
            break
        block = inspect_path(Path(entity))
        raw = inspect_path(Path("/dev/r" + entity[5:]))
        for label, path, information, expected in (
            ("block-", entity, block, stat.S_IFBLK),
            ("raw-", "/dev/r" + entity[5:], raw, stat.S_IFCHR),
        ):
            state = information if isinstance(information, str) else "bound"
            identity = (
                None if isinstance(information, str) else file_identity(information)
            )
            if identity is not None and (
                identity[0] != expected
                or (
                    label == "raw-"
                    and not isinstance(block, str)
                    and identity[3] != block.st_rdev
                )
            ):
                state = "identity_mismatch"
            targets.append(BoundTarget(label + entity[5:], Path(path), identity, state))
        if (
            entity == volume
            and not isinstance(block, str)
            and stat.S_ISBLK(block.st_mode)
        ):
            volume_device = block.st_rdev & 0xFFFFFFFF
    observed = inspect_path(mountpoint)
    euid = os.geteuid()
    capture = classify_volume(observed, volume_device, None, owner_device, euid)
    identity = (
        (observed.st_dev & 0xFFFFFFFF, observed.st_ino)
        if capture == "mounted" and not isinstance(observed, str)
        else None
    )
    return Binding(
        tuple(targets),
        mountpoint,
        volume_device,
        identity,
        owner_device,
        euid,
        capture,
        selected > 8,
    )


def revalidate(binding: Binding) -> tuple[str, tuple[str, ...]]:
    states: list[str] = []
    for target in binding.targets:
        observed = inspect_path(target.path)
        states.append(
            observed
            if isinstance(observed, str)
            else (
                target.capture
                if target.capture != "bound"
                else "bound"
                if file_identity(observed) == target.identity
                else "changed"
            )
        )
    volume = (
        "unmounted"
        if binding.mountpoint is None
        else classify_volume(
            inspect_path(binding.mountpoint),
            binding.volume_device,
            binding.root_identity,
            binding.owner_device,
            binding.euid,
        )
    )
    return volume, tuple(states)


def comm_label(raw: bytes) -> str:
    value = raw[:16].split(b"\0", 1)[0]
    return (
        value.decode("ascii")
        if re.fullmatch(rb"[A-Za-z0-9._+-]{1,16}", value)
        else "unprintable"
    )


def executable_label(path: str) -> str:
    if path == "fixture-python":
        return path
    if (
        len(path) > 256
        or not path.startswith(SYSTEM_ROOTS)
        or any(
            re.fullmatch(r"[A-Za-z0-9._+ -]+", part) is None or part in {".", ".."}
            for part in path.split("/")[1:]
        )
    ):
        return "other"
    return path.replace(" ", "%20")


def elapsed_line(observation: Observation) -> str:
    return "elapsed_ms " + " ".join(
        f"{name}={min(observation.timings.get(name, 0), 99999999):08d}"
        for name in (
            "init",
            "revalidate",
            "pass",
            "entries",
            "final",
            "render",
            "total",
        )
    )


def render(note: Observation, limits: HolderLimits) -> str:
    tokens = [f"{key}.{count}" for key, count in sorted(note.errors.items())]
    kept_errors: list[str] = []
    for token in tokens:
        if len(",".join([*kept_errors, token])) > 480:
            break
        kept_errors.append(token)
    errors = ",".join(kept_errors) or "none"
    if len(kept_errors) < len(tokens):
        errors += f",+{len(tokens) - len(kept_errors)}"
    reserved = [
        f"holder_observation v1 binding={note.binding} processes={note.processes} "
        f"visited={note.visited}/{note.processes} same_uid_processes={note.same_uid} "
        f"other_uid_processes={note.other_uid} holders={note.holder_count}"
        + (" query=" + note.query_failure if note.query_failure else ""),
        elapsed_line(note),
        "errors layout="
        + (",".join(sorted(note.layouts)) or "none")
        + " transient="
        + errors,
        f"target=volume capture={note.capture} before={note.before} "
        f"after={note.after} matching=" + ("on" if note.matching else "off"),
        *(
            f"target={name} before={before} after={after}"
            for name, before, after in note.targets
        ),
        "scope=cwd,root,fds,mapped-volume thread-cwd=unchecked "
        "mapped-device=unchecked visibility=same-uid",
        "volume_entries=" + note.entries,
    ]
    details: list[str] = []
    if note.binding == "unchanged":
        for holder in note.holders:
            line = f"holder pid={holder.pid} identity={holder.identity}"
            if holder.identity == "stable":
                references = ",".join(holder.references)
                if holder.reference_count > len(holder.references):
                    references += f",+{holder.reference_count - len(holder.references)}"
                line += (
                    f" uniqueid={holder.unique} version={holder.version} "
                    f"uid={holder.uid} "
                    f"comm={comm_label(holder.comm)} role={holder.role} "
                    f"executable={executable_label(holder.executable)} "
                    f"after_detach={holder.after_detach} "
                    f"start={holder.start[0]}.{holder.start[1]} references={references}"
                )
                if holder.errors:
                    line += " errors=" + ",".join(holder.errors)
            details.append(line)
    for prefix, (instances, pid, unique, uid) in note.candidates.items():
        details.append(
            f"candidate prefix={prefix} instances={instances} newest_pid={pid} "
            f"uniqueid={unique} uid={uid} after_detach="
            + after_detach(unique, note.detach_unique)
        )
    truncations = set(note.truncated)
    kept: list[str] = []
    for line in details:
        if (
            len(
                "\n".join(
                    [
                        *reserved,
                        *kept,
                        line,
                        "truncated=" + ",".join(sorted(truncations | {"note"})),
                    ]
                )
            )
            <= limits.note
        ):
            kept.append(line)
        else:
            truncations.add("note")
    result = "\n".join(
        [*reserved, *kept, "truncated=" + (",".join(sorted(truncations)) or "none")]
    )
    if len(result) > limits.note:
        raise ValueError("holder note limit cannot retain reserved fields")
    return result


def after_detach(unique: int, detach_unique: int | None) -> str:
    return (
        "unknown"
        if detach_unique is None
        else "yes"
        if unique > detach_unique
        else "no"
    )


class Native:
    def __init__(self) -> None:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        listpids = library.proc_listallpids
        listpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        listpids.restype = ctypes.c_int
        pidinfo = library.proc_pidinfo
        pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        pidinfo.restype = ctypes.c_int
        fdinfo = library.proc_pidfdinfo
        fdinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        fdinfo.restype = ctypes.c_int
        pidpath = library.proc_pidpath
        pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        pidpath.restype = ctypes.c_int
        self.listpids: Callable[[object, int], int] = listpids
        self.pidinfo: Callable[[int, int, int, object, int], int] = pidinfo
        self.fdinfo: Callable[[int, int, int, object, int], int] = fdinfo
        self.pidpath: Callable[[int, object, int], int] = pidpath

    def query(
        self, pid: int, flavor: int, size: int, argument: int = 0
    ) -> tuple[bytes | None, str | None]:
        buffer = ctypes.create_string_buffer(size)
        ctypes.set_errno(0)
        if flavor == -1:
            count = self.listpids(buffer, len(buffer))
        elif flavor == -2:
            count = self.fdinfo(pid, argument, 1, buffer, len(buffer))
        elif flavor == -3:
            count = self.pidpath(pid, buffer, len(buffer))
        else:
            count = self.pidinfo(pid, flavor, argument, buffer, len(buffer))
        code = ctypes.get_errno()
        if flavor == -1 and count <= 0:
            return None, error_name(code)
        if count <= 0 and code:
            return None, error_name(code)
        if flavor == -1 and 0 < count <= size // 4:
            return buffer.raw[: count * 4], None
        if flavor == 1 and 0 <= count <= size and count % 8 == 0:
            return buffer.raw[:count], None
        if flavor == -3 and 0 < count < size:
            return buffer.raw[:count], None
        if flavor not in {-1, -3, 1} and count == size:
            return buffer.raw, None
        return None, "layout"


class Scan:
    def __init__(
        self,
        binding: Binding,
        context: ObservationContext,
        limits: HolderLimits,
        clock: Callable[[], float],
        deadline: float,
        observation: Observation,
    ) -> None:
        self.binding = binding
        self.context = context
        self.limits = limits
        self.clock = clock
        self.deadline = deadline
        self.observation = observation
        self.native = Native()
        self.own: ProcessIdentity | None = None
        self.current_errors: set[str] = set()

    def expired(self) -> bool:
        if self.clock() >= self.deadline:
            self.observation.truncated.add("deadline")
            return True
        return False

    def query(
        self, pid: int, flavor: int, size: int, argument: int = 0
    ) -> bytes | None:
        data, error = self.native.query(pid, flavor, size, argument)
        names = {
            -1: "listallpids",
            -2: "fdinfo",
            -3: "pidpath",
            1: "fds",
            3: "bsd",
            9: "cwd",
            13: "short",
            17: "identity",
            23: "mapped",
        }
        name = names[flavor]
        if flavor == -1 and error is not None:
            self.observation.query_failure = name + "." + error
        if error == "layout":
            self.observation.layouts.add(name)
        if error is not None and not (flavor == 23 and error == "EINVAL"):
            token = name + "." + error
            self.observation.errors[token] = self.observation.errors.get(token, 0) + 1
            if not (flavor == -2 and error in {"EBADF", "ENOENT"}):
                self.current_errors.add(token)
        return data

    def identity(self, pid: int) -> ProcessIdentity | None:
        data = self.query(pid, 17, 56)
        return (
            None
            if data is None
            else (
                number(data, "@Q", 16),
                number(data, "@Q", 24),
                number(data, "@i", 32),
            )
        )

    def references(self, data: bytes, offset: int, label: str) -> tuple[str, ...]:
        device, inode = number(data, "@I", offset), number(data, "@Q", offset + 8)
        result = [
            label + "/" + target.name
            for target in self.binding.targets
            if target.identity is not None
            and target.capture == "bound"
            and target.identity[1:3] == (device, inode)
        ]
        if self.observation.matching and device == self.binding.volume_device:
            result.append(label + "/volume")
        return tuple(result)

    def process_references(self, pid: int) -> tuple[tuple[str, ...], int]:
        references: list[str] = []
        count = 0

        def retain(values: tuple[str, ...]) -> None:
            nonlocal count
            count += len(values)
            references.extend(
                values[: max(0, self.limits.references - len(references))]
            )

        directories = self.query(pid, 9, 2352)
        if directories is not None:
            retain(self.references(directories, 0, "cwd"))
            retain(self.references(directories, 1176, "root"))
        descriptors = self.query(pid, 1, self.limits.descriptors * 8)
        if descriptors is not None:
            if len(descriptors) == self.limits.descriptors * 8:
                self.observation.truncated.add("fds")
            queried = 0
            for offset in range(0, len(descriptors), 8):
                if number(descriptors, "@I", offset + 4) != 1:
                    continue
                descriptor = number(descriptors, "@i", offset)
                data = self.query(pid, -2, 176, descriptor)
                if data is not None and not number(data, "@I", 0) & 0x8000:
                    kind = {
                        stat.S_IFREG: "REG",
                        stat.S_IFDIR: "DIR",
                        stat.S_IFCHR: "CHR",
                        stat.S_IFBLK: "BLK",
                        stat.S_IFLNK: "LNK",
                    }.get(stat.S_IFMT(number(data, "@H", 28)), "OTHER")
                    retain(self.references(data, 24, f"fd/{descriptor}/{kind}"))
                queried += 1
                if queried % self.limits.descriptor_check == 0 and self.expired():
                    break
        if "deadline" not in self.observation.truncated and self.observation.matching:
            mapped = self.query(pid, 23, 1272, self.binding.volume_device or 0)
            if mapped is not None:
                retain(self.references(mapped, 96, "mapped"))
        return tuple(references), count

    def visit(self, pid: int) -> None:
        self.current_errors = set()
        first = self.identity(pid)
        short = self.query(pid, 13, 64)
        references: tuple[str, ...] = ()
        count = 0
        uid = -1 if short is None else number(short, "@I", 36)
        full: bytes | None = None
        executable = "other"
        if uid == self.binding.euid:
            self.observation.same_uid += 1
            references, count = self.process_references(pid)
            if count and len(self.observation.holders) < self.limits.holders:
                full = self.query(pid, 3, 136)
                path = self.query(pid, -3, 4096)
                if path is not None:
                    try:
                        decoded = path.split(b"\0", 1)[0].decode("ascii")
                    except UnicodeDecodeError:
                        decoded = "other"
                    executable = (
                        "fixture-python"
                        if decoded == self.context.executable
                        else decoded
                    )
        elif uid >= 0:
            self.observation.other_uid += 1
        last = self.identity(pid)
        identity = (
            "exited"
            if last is None
            else "stable"
            if first is not None and first == last
            else "reused"
        )
        if full is not None and number(full, "@I", 20) != uid:
            identity = "reused"
        if identity == "stable" and first is not None and short is not None:
            comm = comm_label(short[16:32])
            for prefix in PREFIXES:
                if comm.startswith(prefix[:15]):
                    old = self.observation.candidates.get(prefix, (0, 0, 0, 0))
                    newest = (pid, first[0], uid) if first[0] > old[2] else old[1:]
                    self.observation.candidates[prefix] = (old[0] + 1, *newest)
        if count:
            self.observation.holder_count += 1
            if len(self.observation.holders) < self.limits.holders:
                unique, parent, version = first or (0, 0, 0)
                role = (
                    "self"
                    if pid == self.context.pid
                    else "fixture-descendant"
                    if self.own is not None and parent == self.own[0]
                    else "other"
                )
                self.observation.holders.append(
                    Holder(
                        pid,
                        identity,
                        unique,
                        version,
                        uid,
                        b"" if full is None else full[48:64],
                        executable,
                        role,
                        after_detach(unique, self.context.detach_unique),
                        references,
                        count,
                        tuple(sorted(self.current_errors)),
                        (0, 0)
                        if full is None
                        else (number(full, "@Q", 120), number(full, "@Q", 128)),
                    )
                )
            else:
                self.observation.truncated.add("holders")

    def run(self) -> None:
        if self.expired():
            return
        pids = self.query(0, -1, self.limits.processes * 4)
        if pids is None:
            return
        self.observation.processes = len(pids) // 4
        if self.observation.processes == self.limits.processes:
            self.observation.truncated.add("census")
        self.own = self.identity(self.context.pid)
        for offset in range(0, len(pids), 4):
            if self.expired():
                break
            self.visit(number(pids, "@i", offset))
            self.observation.visited += 1

    def entries(self) -> str:
        if (
            self.expired()
            or not self.observation.matching
            or self.binding.mountpoint is None
        ):
            return "unbound"
        try:
            descriptor = os.open(
                self.binding.mountpoint,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                information = os.fstat(descriptor)
                if (
                    information.st_dev & 0xFFFFFFFF,
                    information.st_ino,
                ) != self.binding.root_identity:
                    return "identity_mismatch"
                entries: list[str] = []
                for name in ENTRY_NAMES:
                    try:
                        mode = os.stat(
                            name, dir_fd=descriptor, follow_symlinks=False
                        ).st_mode
                    except FileNotFoundError:
                        kind = "absent"
                    else:
                        kind = (
                            "dir"
                            if stat.S_ISDIR(mode)
                            else "file"
                            if stat.S_ISREG(mode)
                            else "link"
                            if stat.S_ISLNK(mode)
                            else "other"
                        )
                    entries.append(name + "=" + kind)
                return ",".join(entries)
            finally:
                os.close(descriptor)
        except PermissionError:
            return "denied"
        except OSError as error:
            return "error." + error_name(error.errno or 0)


class HolderObserver:
    def __init__(
        self,
        limits: HolderLimits | None = None,
        budget_clock: Callable[[], float] = time.monotonic,
        diagnostic_clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.limits = limits if limits is not None else HolderLimits()
        self.budget_clock = budget_clock
        self.diagnostic_clock = diagnostic_clock

    def observe(self, binding: Binding, context: ObservationContext) -> str:
        started = self.diagnostic_clock()
        deadline = self.budget_clock() + self.limits.seconds
        if sys.platform != "darwin":
            return "holder_observation v1 status=unsupported_platform"
        observation = Observation(
            capture=binding.capture, detach_unique=context.detach_unique
        )
        scan = Scan(
            binding, context, self.limits, self.budget_clock, deadline, observation
        )
        previous = started

        def timing(name: str) -> None:
            nonlocal previous
            current = self.diagnostic_clock()
            observation.timings[name] = max(0, round((current - previous) * 1000))
            previous = current

        timing("init")
        before, first = revalidate(binding)
        observation.before = before
        observation.matching = volume_matching(binding.capture, before, before)
        if binding.entities_truncated:
            observation.truncated.add("entities")
        timing("revalidate")
        scan.run()
        timing("pass")
        observation.entries = scan.entries()
        timing("entries")
        after, last = revalidate(binding)
        observation.after = after
        observation.targets = tuple(
            (target.name, a, b)
            for target, a, b in zip(binding.targets, first, last, strict=True)
        )
        capture = "unmounted" if binding.mountpoint is None else binding.capture
        observation.binding = (
            "changed"
            if binding_changed(
                capture, before, after, tuple(zip(first, last, strict=True))
            )
            else "unchanged"
        )
        observation.matching = volume_matching(binding.capture, before, after)
        if not observation.matching:
            observation.entries = "unbound"
        timing("final")
        line = elapsed_line(observation)
        result = render(observation, self.limits)
        timing("render")
        observation.timings["total"] = max(0, round((previous - started) * 1000))
        return result.replace(line, elapsed_line(observation), 1)
