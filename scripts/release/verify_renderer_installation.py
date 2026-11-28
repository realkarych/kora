import errno
import fcntl
import os
import secrets
import select
import signal
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from io import FileIO
from threading import Event, RLock
from typing import Never, TypedDict

if __name__ == "__main__":
    sys.path[:0] = ["/run/kora-preparer", "/run/kora-preparer/support"]

from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    LoggingSettings,
    configure_process,
)
from scripts.release.renderer_installation_check import (
    ANCHOR,
    CHUNK,
    DIRECTORY,
    FILE,
    IMAGE,
    KERNEL,
    MAX_DOCUMENTS,
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_HEADER,
    MAX_INDEX,
    MAX_JSON_STRUCTURE,
    MAX_MANIFEST,
    MAX_RESERVATION,
    PROC_LINE,
    READ_REFUSAL_EXIT,
    ROLES,
    Refusal,
    Root,
    canonical,
    decoded,
    deployment,
    device,
    frame_size,
    hex_digest,
    identity,
    integer,
    keys,
    mount_rows,
    mounts,
    namespace,
    require,
)

DM_HEADER = struct.Struct("<6IiIIIQ")
TARGET = struct.Struct("<QQiI16s")
DM_HEADER_SIZE = 312
DM_FLAGS = 1 | 32
DM_UNSAFE = 2 | 64 | 256 | (1 << 17) | (1 << 18)
DM_REMOVE, DM_STATUS, DM_TABLE = 4, 7, 12
LOOP_FREE, LOOP_ATTACH, LOOP_INFO, LOOP_CLEAR = 0x4C82, 0x4C0A, 0x4C05, 0x4C01
BLK_READ_ONLY = 0x125E
ENV = {"LC_ALL": "C"}
POLL = 0.02
DEADLINE_NS = 15_000_000_000
TERM_NS = 500_000_000
SETTLEMENT_NS = 2_000_000_000
FAILURES = (Refusal, OSError, ValueError, KeyError)
PIPE_PAGES = 16
JSON_SCRATCH = 2 * (8 * (MAX_MANIFEST + MAX_HEADER) + 1024 * MAX_JSON_STRUCTURE)
DIRECTORY_SCRATCH = 1024 * 1024 + 4096
IO_SCRATCH = 12 * CHUNK
PROC_SCRATCH = 128 * PROC_LINE
LOG_RECORDS, LOG_RECORD_BYTES = 16, 4096
LOG_QUEUE = LOG_RECORDS * LOG_RECORD_BYTES
LOG_SCRATCH = 2 * LOG_QUEUE + 4 * LOG_RECORD_BYTES
NATIVE_SCRATCH = 256 * 1024
CONTROL_SCRATCH = IO_SCRATCH + PROC_SCRATCH + LOG_SCRATCH + NATIVE_SCRATCH


def opened(owned: ExitStack, path: str, flags: int, dir_fd: int | None = None) -> int:
    fd = os.open(path, flags, dir_fd=dir_fd)
    owned.callback(os.close, fd)
    return fd


def owned_directory(fd: int) -> None:
    entry = os.fstat(fd)
    require(entry.st_uid == 0 and entry.st_mode & 0o077 == 0, "anchor")


def length(fd: int) -> int:
    entry = os.fstat(fd)
    require(stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1, "input")
    return entry.st_size


class Job:
    def __init__(self) -> None:
        self.deadline_ns = time.monotonic_ns() + DEADLINE_NS
        self.lock = RLock()
        self.refusals: dict[str, tuple[str, int]] = {}
        self.finished = False
        self.active: subprocess.Popen[bytes] | None = None
        self.releasing_after_refusal = False
        self.signalled = 0

    def interrupt(self, signum: int, frame: object) -> None:
        with self.lock:
            if not self.finished:
                self._refuse(time.monotonic_ns(), "cancelled")

    @property
    def refusal(self) -> str | None:
        return self.refusals.get("first", (None, 0))[0]

    @property
    def first_refusal(self) -> int:
        return self.refusals.get("first", (None, 0))[1]

    def _refuse(self, now: int, reason: str) -> None:
        self.refusals.setdefault("first", (reason, now))

    def _observe(self, now: int) -> None:
        if now >= self.deadline_ns:
            self._refuse(now, "deadline")
        active = self.active
        if (
            self.refusal is not None
            and active is not None
            and not self.releasing_after_refusal
            and active.poll() is None
        ):
            if now - self.first_refusal >= TERM_NS and self.signalled < 2:
                active.kill()
                self.signalled = 2
            elif self.signalled == 0:
                active.terminate()
                self.signalled = 1

    def observe(self) -> None:
        with self.lock:
            self._observe(time.monotonic_ns())

    def _admit(self) -> None:
        self._observe(time.monotonic_ns())
        if self.refusal is not None:
            raise Refusal(self.refusal)

    def admit(self) -> None:
        with self.lock:
            self._admit()

    def finish(self) -> None:
        with self.lock:
            self.finished = True
            self._admit()

    @contextmanager
    def checked(self) -> Generator[None, None, None]:
        try:
            yield
        except FAILURES as error:
            with self.lock:
                now = time.monotonic_ns()
                self._observe(now)
                self._refuse(
                    now, str(error) if isinstance(error, Refusal) else "unavailable"
                )
            raise

    @contextmanager
    def resources(self) -> Generator[ExitStack, None, None]:
        with self.checked(), ExitStack() as owned, self.checked():
            yield owned

    def activate(self, descriptor: int, final: bytes) -> None:
        require(len(final) == 1, "channel")
        while True:
            with self.lock:
                self._admit()
                try:
                    require(os.write(descriptor, final) == 1, "channel")
                except BlockingIOError:
                    pass
                else:
                    return
            select.select((), (descriptor,), (), POLL)

    def settle_result(
        self, child: subprocess.Popen[bytes], result: tuple[int, int] | None
    ) -> tuple[int, int]:
        while True:
            with self.lock:
                require(self.active is child, "settlement")
                status = child.poll()
                self._admit()
                if status is not None:
                    if result is not None and status == 0:
                        return result
                    reason = "read" if status == READ_REFUSAL_EXIT else "channel"
                    if result is not None:
                        reason = "child"
                    self._refuse(time.monotonic_ns(), reason)
                    raise Refusal(reason)
            select.select((), (), (), POLL)

    @contextmanager
    def own(
        self,
        child: subprocess.Popen[bytes],
        releasing: bool = False,
        issued_ns: int = 0,
    ) -> Generator[subprocess.Popen[bytes], None, None]:
        with self.lock:
            self.active = child
            self.releasing_after_refusal = (
                releasing
                and self.refusal is not None
                and issued_ns >= self.first_refusal
            )
            self.signalled = 0
        try:
            with self.checked():
                yield child
        finally:
            try:
                child.wait()
            finally:
                if child.returncode is None:
                    self.retain_for_guest_retirement()
                with self.lock:
                    self.active = None
                for channel in (child.stdin, child.stdout, child.stderr):
                    if channel is not None:
                        channel.close()

    def execute(
        self,
        child: subprocess.Popen[bytes],
        releasing: bool = False,
        issued_ns: int = 0,
    ) -> None:
        with self.own(child, releasing, issued_ns):
            if not releasing:
                self.admit()
            require(child.wait() == 0, "settlement" if releasing else "native")

    def release(self, operation: Callable[[], None]) -> None:
        try:
            with self.checked():
                operation()
        except FAILURES:
            self.retain_for_guest_retirement()

    def retain_for_guest_retirement(self) -> Never:
        with self.lock:
            self._refuse(time.monotonic_ns(), "settlement")
        retired = Event()
        while True:
            retired.wait()

    def observation(self) -> tuple[float, str | None]:
        with self.lock:
            now = time.monotonic_ns()
            self._observe(now)
            if self.refusal is None:
                return POLL, None
            remaining = self.first_refusal + SETTLEMENT_NS - now
            if remaining > 0:
                return min(POLL, remaining / 1_000_000_000), None
            return POLL, self.refusal


def loop_state(fd: int) -> tuple[str, int, int, int] | None:
    raw = bytearray(232)
    try:
        fcntl.ioctl(fd, LOOP_INFO, raw)
    except OSError as error:
        if error.errno != errno.ENXIO:
            raise
        return None
    dev, inode, _, offset, limit, _, _, _, flags = struct.unpack_from("<5Q4I", raw)
    return device(dev), inode, offset | limit, flags


def release_loop(
    handle: FileIO, path: str, expected: tuple[str, int, int, int]
) -> None:
    fd = handle.fileno()
    original = os.fstat(fd).st_rdev
    attached = loop_state(fd)
    if attached is not None:
        require(attached == expected, "settlement")
        fcntl.ioctl(fd, LOOP_CLEAR)
    handle.close()
    with ExitStack() as owned:
        current = opened(owned, path, FILE)
        require(
            os.fstat(current).st_rdev == original and loop_state(current) is None,
            "settlement",
        )


def attach(owned: ExitStack, job: Job, ctl: int, backing: int) -> str:
    path = f"/dev/loop{fcntl.ioctl(ctl, LOOP_FREE)}"
    handle = owned.enter_context(FileIO(os.open(path, FILE), "rb"))
    fd = handle.fileno()
    value = os.fstat(backing)
    expected = (device(value.st_dev), value.st_ino, 0, 1)
    require(loop_state(fd) is None and stat.S_ISBLK(os.fstat(fd).st_mode), "native")
    job.admit()
    owned.callback(job.release, partial(release_loop, handle, path, expected))
    config = bytearray(304)
    struct.pack_into("<II", config, 0, backing, 0)
    struct.pack_into("<I", config, 60, 1)
    fcntl.ioctl(fd, LOOP_ATTACH, config)
    require(loop_state(fd) == expected, "native")
    return device(os.fstat(fd).st_rdev)


def dm_ioctl(
    fd: int, operation: int, uuid: str, flags: int = 0
) -> tuple[int, int, int, bytes]:
    buffer = bytearray(4096)
    DM_HEADER.pack_into(
        buffer, 0, 4, 0, 0, len(buffer), DM_HEADER_SIZE, 0, 0, flags, 0, 0, 0
    )
    raw_uuid = uuid.encode("ascii")
    buffer[176 : 176 + len(raw_uuid)] = raw_uuid
    fcntl.ioctl(fd, 0xC0000000 | DM_HEADER_SIZE << 16 | 0xFD << 8 | operation, buffer)
    major, _, _, size, start, count, _, state, _, _, dev = DM_HEADER.unpack_from(buffer)
    require(
        major == 4
        and 305 <= size <= len(buffer)
        and DM_HEADER_SIZE <= start <= len(buffer)
        and not state & 256,
        "native",
    )
    require(bytes(buffer[176:305]).split(b"\0", 1)[0] == raw_uuid, "native")
    return dev, state, count, bytes(buffer[start:size])


def dm_target(fd: int, uuid: str, query: int, blocks: int) -> tuple[int, str]:
    dev, flags, count, raw = dm_ioctl(fd, DM_TABLE, uuid, query)
    require(
        flags & DM_FLAGS == DM_FLAGS
        and not flags & DM_UNSAFE
        and count == 1
        and len(raw) > TARGET.size,
        "verity",
    )
    start, sectors, status, next_offset, kind = TARGET.unpack_from(raw)
    require(
        start == 0
        and sectors == blocks * 8
        and status == 0
        and next_offset == ((len(raw) + 7) & ~7)
        and kind.rstrip(b"\0") == b"verity",
        "verity",
    )
    message, separator, extra = raw[TARGET.size :].partition(b"\0")
    require(bool(separator) and not extra, "verity")
    return dev, message.decode("ascii")


def dm_release(fd: int, uuid: str) -> None:
    try:
        dm_ioctl(fd, DM_REMOVE, uuid)
    except OSError as error:
        if error.errno != errno.ENXIO:
            raise
    try:
        dm_ioctl(fd, DM_STATUS, uuid)
    except OSError as error:
        if error.errno == errno.ENXIO:
            return
        raise
    raise Refusal("settlement")


@dataclass
class MountOwner:
    role: int
    path: str
    dev: str
    original_namespace: tuple[int, int]
    number: int | None = None
    issued: bool = False

    def current(self) -> int | None:
        require(namespace() == self.original_namespace, "settlement")
        path, dev = os.fsencode(self.path), self.dev.encode("ascii")
        found: int | None = None
        with closing(mount_rows()) as rows:
            for row in rows:
                if row.path == path or row.dev == dev or row.number == self.number:
                    require(
                        found is None
                        and row.path == path
                        and row.dev == dev
                        and (self.number is None or row.number == self.number),
                        "settlement",
                    )
                    found = row.number
        return found

    def acquire(self, job: Job) -> None:
        require(self.current() is None, "root")
        job.admit()
        self.issued = True
        try:
            with job.checked():
                mount_role(job, self.role)
        finally:
            self.number = self.current()
        require(self.number is not None, "root")

    def unmount(self, job: Job) -> None:
        while self.issued and self.current() is not None:
            require(self.number is not None, "settlement")
            try:
                unmount(job, self.role)
            except Refusal:
                interval, pending = job.observation()
                if pending is not None and self.current() is not None:
                    raise
                select.select((), (), (), interval)
            else:
                require(self.current() is None, "settlement")
                return


class QuietProcess(TypedDict):
    stdout: int
    stderr: int
    env: dict[str, str]


QUIET: QuietProcess = {
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
    "env": ENV,
}


def mount_role(job: Job, role: int) -> None:
    if role == 0:
        proc = subprocess.Popen(
            [
                "/usr/bin/mount",
                "-text4",
                "-oro,noload",
                "/dev/mapper/kora-renderer-verifier",
                "/run/kora-preparation/roots/verifier",
            ],
            **QUIET,
        )
    else:
        proc = subprocess.Popen(
            [
                "/usr/bin/mount",
                "-text4",
                "-oro,noload",
                "/dev/mapper/kora-renderer-candidate",
                "/run/kora-preparation/roots/candidate",
            ],
            **QUIET,
        )
    job.execute(proc)


def unmount(job: Job, role: int) -> None:
    job.observe()
    issued_ns = time.monotonic_ns()
    if role == 0:
        proc = subprocess.Popen(
            ["/usr/bin/umount", "/run/kora-preparation/roots/verifier"], **QUIET
        )
    else:
        proc = subprocess.Popen(
            ["/usr/bin/umount", "/run/kora-preparation/roots/candidate"], **QUIET
        )
    job.execute(proc, releasing=True, issued_ns=issued_ns)


def mapped(dm: int, uuid: str, shape: Root, table: str) -> int:
    dev, state, count, _ = dm_ioctl(dm, DM_STATUS, uuid)
    require(
        state & DM_FLAGS == DM_FLAGS and not state & DM_UNSAFE and count == 1, "verity"
    )
    table_dev, found = dm_target(dm, uuid, 1 << 4, shape.blocks)
    status_dev, valid = dm_target(dm, uuid, 0, shape.blocks)
    require(
        dev == table_dev == status_dev and found == table and valid == "V", "verity"
    )
    return dev


def observe(
    fd: int, block: int, dm: int, shape: Root, path: str, uuid: str, table: str
) -> tuple[int, str, int]:
    dev = mapped(dm, uuid, shape, table)
    disk = os.fstat(block)
    root = os.fstat(fd)
    require(
        stat.S_ISBLK(disk.st_mode)
        and disk.st_rdev == root.st_dev == dev
        and root == os.stat(path, follow_symlinks=False),
        "root",
    )
    read_only = bytearray(4)
    fcntl.ioctl(block, BLK_READ_ONLY, read_only)
    require(struct.unpack("<i", read_only) == (1,), "verity")
    superblock = os.pread(block, 1024, 1024)
    require(
        len(superblock) == 1024
        and superblock[56:58] == b"\x53\xef"
        and superblock[24:28] == b"\x02\0\0\0"
        and not superblock[92] & 4,
        "verity",
    )
    return identity(fd)


def prepare_root(
    job: Job, dm: int, uuid: str, role: str, shape: Root, table: str
) -> int:
    name = f"kora-renderer-{role}"
    mapper = f"/dev/mapper/{name}"
    require(not os.path.lexists(mapper), "root")
    job.admit()
    request = f"{name},{uuid},,ro,0 {shape.blocks * 8} verity {table}".encode("ascii")
    with ExitStack() as owned:
        source, sink = pipe(owned, reserve())
        proc = subprocess.Popen(
            ["/usr/sbin/dmsetup", "create", "--concise"], stdin=source, **QUIET
        )
        with job.own(proc), sink:
            source.close()
            os.set_blocking(sink.fileno(), False)
            write_pipe(job, sink.fileno(), request)
        require(proc.returncode == 0, "native")
    dev = mapped(dm, uuid, shape, table)
    require(os.stat(mapper).st_rdev == dev, "verity")
    return dev


def child_match(
    job: Job,
    roots: tuple[int, int],
    metadata: bytes,
    files: tuple[int, int, int],
    sizes: tuple[int, int, int],
    correlation: str,
    selector: str,
) -> tuple[int, int]:
    with job.resources() as owned:
        pipe_budget = reserve()
        child_input, sink = pipe(owned, pipe_budget)
        source, child_output = pipe(owned, pipe_budget)
        request, result = child_input.fileno(), child_output.fileno()
        env = {
            **ENV,
            "KORA_PREP_DEADLINE_NS": str(job.deadline_ns),
            "KORA_PREP_REQ": str(request),
            "KORA_PREP_RESULT": str(result),
            "KORA_PREP_VERIFIER": str(roots[0]),
            "KORA_PREP_CANDIDATE": str(roots[1]),
        }
        job.admit()
        with job.own(
            subprocess.Popen(
                [
                    "/usr/bin/setpriv",
                    "--reuid=65532",
                    "--regid=65532",
                    "--clear-groups",
                    "--inh-caps=-all",
                    "--ambient-caps=-all",
                    "--bounding-set=-all",
                    "--no-new-privs",
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-B",
                    "/run/kora-preparer/scripts/release/renderer_installation_check.py",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env=env,
                close_fds=True,
                pass_fds=(request, result, *roots),
            )
        ) as proc:
            child_input.close()
            child_output.close()
            with sink:
                job.admit()
                writer = sink.fileno()
                os.set_blocking(writer, False)
                write_pipe(job, writer, frame_size(len(metadata), sizes))
                write_pipe(job, writer, metadata)
                for index, (fd, size) in enumerate(zip(files, sizes, strict=True)):
                    transfer(job, fd, size, writer, index == len(files) - 1)
            reader = source.fileno()
            os.set_blocking(reader, False)
            return receive(job, proc, reader, correlation, selector)


def reserve(*, budget: int = MAX_RESERVATION) -> int:
    pipe_budget = PIPE_PAGES * os.sysconf("SC_PAGESIZE")
    scratch = JSON_SCRATCH + DIRECTORY_SCRATCH + CONTROL_SCRATCH
    needed = MAX_DOCUMENTS + MAX_INDEX + scratch + 2 * pipe_budget
    require(pipe_budget > 0 and needed <= budget <= MAX_RESERVATION, "capacity")
    return pipe_budget


def pipe(owned: ExitStack, budget: int) -> tuple[FileIO, FileIO]:
    reader, writer = os.pipe()
    source = owned.enter_context(FileIO(reader, "rb"))
    sink = owned.enter_context(FileIO(writer, "wb"))
    require(0 < fcntl.fcntl(reader, fcntl.F_GETPIPE_SZ) <= budget, "capacity")
    return source, sink


def write_pipe(job: Job, fd: int, data: bytes) -> None:
    pending = memoryview(data)
    while pending:
        job.admit()
        try:
            count = os.write(fd, pending)
        except BlockingIOError:
            select.select((), (fd,), (), POLL)
        else:
            require(count > 0, "channel")
            pending = pending[count:]


def transfer(job: Job, fd: int, size: int, writer: int, last: bool) -> None:
    end = size - last
    offset = 0
    while offset < end:
        job.admit()
        data = os.pread(fd, min(end - offset, CHUNK), offset)
        require(bool(data), "input")
        write_pipe(job, writer, data)
        offset += len(data)
    require(not os.pread(fd, 1, size), "input")
    if last:
        job.activate(writer, os.pread(fd, 1, end))


def read_pipe(job: Job, fd: int, length: int) -> bytes:
    while True:
        job.admit()
        try:
            return os.read(fd, length)
        except BlockingIOError:
            select.select((fd,), (), (), POLL)


def read_exact(job: Job, fd: int, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        block = read_pipe(job, fd, length - len(data))
        require(bool(block), "channel")
        data.extend(block)
    return bytes(data)


def receive(
    job: Job,
    child: subprocess.Popen[bytes],
    reader: int,
    correlation: str,
    selector: str,
) -> tuple[int, int]:
    first = read_pipe(job, reader, 1)
    if not first:
        return job.settle_result(child, None)
    length = int.from_bytes(first + read_exact(job, reader, 3), "big")
    require(0 < length <= MAX_HEADER - 4, "channel")
    matched = keys(
        decoded(read_exact(job, reader, length)), "job selector entries bytes"
    )
    require((matched["job"], matched["selector"]) == (correlation, selector), "child")
    entries = integer(matched["entries"], 0, MAX_ENTRIES)
    size = integer(matched["bytes"], 0, MAX_FILE_BYTES)
    del matched
    require(not read_pipe(job, reader, 1), "channel")
    return job.settle_result(child, (entries, size))


def compare(job: Job, selector: str) -> tuple[int, int]:
    with job.resources() as owned:
        require(os.getuid() == os.geteuid() == 0, "boot")
        boot = os.uname()
        require((boot.machine, boot.release) == ("x86_64", KERNEL), "boot")
        init_ns = os.stat("/proc/1/ns/mnt")
        require(namespace() != (init_ns.st_dev, init_ns.st_ino), "root")
        pinned = hex_digest(os.environ.get("KORA_PREPARATION_SELECTOR"))
        require(selector == pinned, "selector")
        require(os.environ.get("KORA_PREPARATION_IMAGE_SHA256") == IMAGE, "selector")
        reserve()
        held_namespace = opened(owned, "/proc/self/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
        ns_stat = os.fstat(held_namespace)
        observed_ns = (ns_stat.st_dev, ns_stat.st_ino)
        require(namespace() == observed_ns, "root")
        anchor = opened(owned, ANCHOR, DIRECTORY)
        owned_directory(anchor)
        registry = opened(owned, "registry", DIRECTORY, anchor)
        owned_directory(registry)
        deployments = opened(owned, "deployments", DIRECTORY, registry)
        inventories = opened(owned, "inventories", DIRECTORY, registry)
        manifest = opened(owned, f"{selector}.json", FILE, deployments)
        manifest_size = length(manifest)
        require(0 < manifest_size <= MAX_MANIFEST, "capacity")
        plan = deployment(os.pread(manifest, manifest_size + 1, 0), selector)
        require(
            plan.boot
            == (
                os.environ.get("KORA_PREPARATION_KERNEL_SHA256"),
                os.environ.get("KORA_PREPARATION_INITRD_SHA256"),
            ),
            "boot",
        )
        first = opened(owned, f"{plan.roots[0].inventory}.json", FILE, inventories)
        second = opened(owned, f"{plan.roots[1].inventory}.json", FILE, inventories)
        sizes = (manifest_size, length(first), length(second))
        require(min(sizes) > 0 and sum(sizes) <= MAX_DOCUMENTS, "capacity")
        media = opened(owned, "media", DIRECTORY, anchor)
        backing = tuple(
            opened(owned, f"{role}.{kind}", FILE, media)
            for role in ROLES
            for kind in ("data", "hash")
        )
        require(
            all(length(fd) > 0 for fd in backing)
            and len({(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in backing})
            == 4,
            "input",
        )
        dm = opened(owned, "/dev/mapper/control", FILE)
        control = opened(owned, "/dev/loop-control", FILE)
        rootbase = opened(owned, "roots", DIRECTORY, anchor)
        owned_directory(rootbase)
        code = secrets.token_hex(32)
        uuids = (f"KORA-{code}-verifier", f"KORA-{code}-candidate")
        paths = (f"{ANCHOR}/roots/verifier", f"{ANCHOR}/roots/candidate")
        devices = tuple(attach(owned, job, control, fd) for fd in backing)
        require(len(set(devices)) == 4, "verity")
        blocks: list[str] = []
        tables: list[str] = []
        mount_owners: list[MountOwner] = []
        for idx, (role, uuid, path, shape) in enumerate(
            zip(ROLES, uuids, paths, plan.roots, strict=True)
        ):
            job.admit()
            data, hashes = (devices[2 * idx], devices[2 * idx + 1])
            geometry = f"1 {data} {hashes} 4096 4096 {shape.blocks} 0 sha256"
            tables.append(f"{geometry} {shape.digest} {shape.salt}")
            require(length(backing[2 * idx]) == shape.blocks * 4096, "input")
            os.mkdir(role, 0o700, dir_fd=rootbase)
            owned.callback(job.release, partial(os.rmdir, role, dir_fd=rootbase))
            owned.callback(job.release, partial(dm_release, dm, uuid))
            dev = prepare_root(job, dm, uuid, role, shape, tables[idx])
            blocks.append(f"/dev/dm-{os.minor(dev)}")
            mount = MountOwner(idx, path, device(dev), observed_ns)
            mount_owners.append(mount)
            owned.callback(job.release, partial(mount.unmount, job))
            mount.acquire(job)
        with job.resources() as roots:
            descriptors = (
                opened(roots, ROLES[0], DIRECTORY, rootbase),
                opened(roots, ROLES[1], DIRECTORY, rootbase),
            )
            disks = tuple(opened(roots, path, FILE) for path in blocks)

            def live() -> tuple[tuple[int, str, int], tuple[int, str, int]]:
                verifier, candidate = (
                    observe(fd, disk, dm, shape, path, uuid, table)
                    for fd, disk, shape, path, uuid, table in zip(
                        descriptors,
                        disks,
                        plan.roots,
                        paths,
                        uuids,
                        tables,
                        strict=True,
                    )
                )
                return verifier, candidate

            before = live()
            require(
                namespace() == observed_ns
                and all(
                    root[0] == owner.number
                    for root, owner in zip(before, mount_owners, strict=True)
                ),
                "root",
            )
            mounts(before)
            metadata = canonical(
                {
                    "job": code,
                    "selector": selector,
                    "namespace": observed_ns,
                    "roots": before,
                    "mappings": [
                        [uuid, root[1], table, "V"]
                        for uuid, root, table in zip(uuids, before, tables, strict=True)
                    ],
                }
            )
            result = child_match(
                job,
                descriptors,
                metadata,
                (manifest, first, second),
                sizes,
                code,
                selector,
            )
            require(live() == before and namespace() == observed_ns, "root")
            mounts(before)
        job.admit()
    job.admit()
    return result


def run(selector: str, report_pending: Callable[[str], None]) -> tuple[int, int]:
    job = Job()
    handlers = {
        sig: signal.signal(sig, job.interrupt)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with ThreadPoolExecutor(max_workers=1) as lane:
            result = lane.submit(compare, job, selector)
            reported = False
            while not result.done():
                interval, refusal = job.observation()
                if refusal is not None and not reported:
                    report_pending(refusal)
                    reported = True
                wait((result,), timeout=interval)
        try:
            return result.result()
        finally:
            job.finish()
    finally:
        for sig, prior in handlers.items():
            signal.signal(sig, prior)


def pending_output(message: str) -> bool:
    try:
        stdout = sys.stdout.fileno()
        expected = os.fstat(stdout)
        mode = fcntl.fcntl(stdout, fcntl.F_GETFL) & os.O_ACCMODE
        if not stat.S_ISFIFO(expected.st_mode) or mode == os.O_RDONLY:
            return False
        flags = os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC
        with FileIO(os.open("/proc/self/fd/1", flags), "wb") as output:
            actual = os.fstat(output.fileno())
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                return False
            raw = message.encode("ascii")
            return output.write(raw) == len(raw)
    except (OSError, ValueError):
        return False


def discard_stdout() -> None:
    fd = os.open(os.devnull, os.O_WRONLY)
    if fd != 1:
        try:
            os.dup2(fd, 1)
        finally:
            os.close(fd)


def main() -> int:
    component = CATALOG.register_component("release.renderer_preparation")
    fields = {
        "phase": FieldKind.STRING,
        "reason": FieldKind.STRING,
        "entries": FieldKind.INTEGER,
        "bytes": FieldKind.INTEGER,
        "settled": FieldKind.BOOLEAN,
    }
    passed, failed = (
        CATALOG.register(EventSpec(component + name, component, level, message, fields))
        for name, level, message in (
            (".completed", Level.INFO, "Static preparation comparison completed"),
            (".refused", Level.WARNING, "Static preparation comparison refused"),
        )
    )
    unhandled = CATALOG.register(
        EventSpec(
            "process.failed",
            component,
            Level.CRITICAL,
            "the process stopped on an unhandled error",
            {"exception": FieldKind.EXCEPTION, "exit_code": FieldKind.INTEGER},
        )
    )
    diagnostics = configure_process(
        component,
        LoggingSettings(
            max_record_bytes=LOG_RECORD_BYTES,
            queue_records=LOG_RECORDS,
            queue_bytes=LOG_QUEUE,
        ),
        secrets.token_hex(16),
        lambda: datetime.now(UTC),
    )
    logger = diagnostics.logger(component)
    logged = announced = False

    def emit_outcome(
        event: EventSpec, reason: str, settled: bool, entries: int = 0, size: int = 0
    ) -> None:
        logger.emit(
            event,
            phase="preparation",
            reason=reason,
            entries=entries,
            bytes=size,
            settled=settled,
        )

    def report(reason: str, pending: bool) -> None:
        nonlocal logged, announced
        if not logged:
            emit_outcome(failed, reason, settled=not pending)
            logged = True
        if pending and not announced and sys.stdout is not None:
            announced = pending_output(f"static_refusal {reason}\n")

    stdout = sys.stdout
    exit_code = 0
    try:
        try:
            require(len(sys.argv) == 2, "selector")
            selector = hex_digest(sys.argv[1])
            entries, size = run(selector, partial(report, pending=True))
        except FAILURES as error:
            reason = str(error) if isinstance(error, Refusal) else "unavailable"
            report(reason, False)
            message = f"static_refusal {reason}\n"
            exit_code = 1
        else:
            emit_outcome(passed, "matched", settled=True, entries=entries, size=size)
            message = f"static_match {selector}\n"
        if stdout is not None and not announced:
            stdout.write(message)
    except BrokenPipeError:
        exit_code = 1
        discard_stdout()
    except KeyboardInterrupt:
        exit_code = 130
    except SystemExit:
        raise
    except BaseException as error:
        logger.emit(unhandled, exception=error, exit_code=70)
        raise SystemExit(70) from error
    finally:
        try:
            if stdout is not None:
                stdout.flush()
        except (OSError, ValueError):
            exit_code = exit_code or 1
            discard_stdout()
        finally:
            diagnostics.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
