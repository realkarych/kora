import array
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import sys
import time
from bisect import bisect_left
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from typing import BinaryIO, cast

ROLES = ("verifier", "candidate")
ANCHOR = "/run/kora-preparation"
MAX_DOCUMENTS = 4 * 1024 * 1024
MAX_INDEX = 8 * 1024 * 1024
MAX_RESERVATION = 16 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PATH = 1024
MAX_MANIFEST = 10 * 1024
MAX_HEADER = 4096
MAX_JSON_STRUCTURE = 64
CHUNK = 32 * 1024
PROC_LINE = 8192
READ_REFUSAL_EXIT = 2
DOMAIN = b"kora.renderer-root-inventory/1\0"
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMPONENT = re.compile(rb"[A-Za-z0-9._+-]+\Z")
ROW = re.compile(
    rb'\{"path":"([^"]+)","sha256":"([0-9a-f]{64})","size_bytes":(0|[1-9][0-9]{0,18})\}'
)
FRAME = struct.Struct("!5I")
PACK = struct.Struct("!IHQ32s")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
RELEASE = "ubuntu-24.04-amd64-20261017"
IMAGE = "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
KERNEL = "6.8.0-147-generic"
CPYTHON = "3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522"
JINJA = "85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67"
MARKUPSAFE = "6da83a088f8ef93b2d483a8232a4dbf4d69d3d8496b568a03c56becac43e1808"
INPUTS = {
    "cpython_sha256": CPYTHON,
    "jinja_sha256": JINJA,
    "markupsafe_sha256": MARKUPSAFE,
}


class Refusal(Exception):
    pass


class ReadRefusal(Refusal):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def hex_digest(value: object) -> str:
    if isinstance(value, str) and HEX.fullmatch(value):
        return value
    raise Refusal("document")


def integer(value: object, lowest: int, highest: int) -> int:
    if type(value) is int and lowest <= value <= highest:
        return value
    raise Refusal("document")


def keys(value: object, fields: str) -> dict[str, object]:
    require(type(value) is dict, "document")
    result = cast(dict[str, object], value)
    require(result.keys() == set(fields.split()), "document")
    return result


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    require(len(result) == len(pairs), "document")
    return result


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def json_number(value: str) -> int:
    require(len(value) <= 20 and value.lstrip("-").isdecimal(), "document")
    return int(value)


def decoded(raw: bytes) -> object:
    structure = sum(raw.count(token) for token in (b"{", b"[", b":", b","))
    require(len(raw) <= MAX_MANIFEST and structure <= MAX_JSON_STRUCTURE, "document")
    require(b"\\" not in raw, "document")
    try:
        value: object = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=unique,
            parse_int=json_number,
            parse_float=json_number,
            parse_constant=json_number,
        )
        require(canonical(value) == raw, "document")
    except (ValueError, RecursionError) as error:
        raise Refusal("document") from error
    return value


def path_bytes(value: object) -> bytes:
    if isinstance(value, str) and value.isascii():
        return checked_path(value.encode("ascii"))
    raise Refusal("document")


def checked_path(value: bytes) -> bytes:
    require(0 < len(value) <= MAX_PATH, "path")
    require(
        all(
            x not in (b".", b"..") and COMPONENT.fullmatch(x) for x in value.split(b"/")
        ),
        "path",
    )
    return value


@dataclass(frozen=True)
class Root:
    inventory: str
    entries: tuple[bytes, bytes, bytes]
    stdlib: bytes
    blocks: int
    digest: str
    salt: str


@dataclass(frozen=True)
class Deployment:
    boot: tuple[str, str]
    roots: tuple[Root, Root]


def root_record(value: object) -> Root:
    row = keys(value, "inventory_sha256 entry interpreter elf_loader stdlib verity")
    entries = (
        path_bytes(row["entry"]),
        path_bytes(row["interpreter"]),
        path_bytes(row["elf_loader"]),
    )
    require(len(set(entries)) == 3, "document")
    verity = keys(row["verity"], "data_blocks root_hash salt")
    return Root(
        hex_digest(row["inventory_sha256"]),
        entries,
        path_bytes(row["stdlib"]),
        integer(verity["data_blocks"], 1, (2**63 - 1) // 4096),
        hex_digest(verity["root_hash"]),
        hex_digest(verity["salt"]),
    )


def deployment(raw: bytes, selector: str) -> Deployment:
    require(
        len(raw) <= MAX_MANIFEST and hashlib.sha256(raw).hexdigest() == selector,
        "selector",
    )
    obj = keys(decoded(raw), "revision base inputs verifier candidate")
    integer(obj["revision"], 1, 1)
    base = keys(
        obj["base"], "release image_sha256 kernel_release kernel_sha256 initrd_sha256"
    )
    require(
        (base["release"], base["image_sha256"], base["kernel_release"])
        == (RELEASE, IMAGE, KERNEL),
        "boot",
    )
    require(obj["inputs"] == INPUTS, "document")
    return Deployment(
        (hex_digest(base["kernel_sha256"]), hex_digest(base["initrd_sha256"])),
        (root_record(obj["verifier"]), root_record(obj["candidate"])),
    )


class InventoryStream:
    def __init__(self, source: BinaryIO, length: int) -> None:
        self.source: BinaryIO = source
        self.unread = length
        self.digest = hashlib.sha256(DOMAIN)
        self.chunk: bytes = b""
        self.offset = 0

    def fill(self) -> None:
        if self.offset == len(self.chunk):
            require(self.unread > 0, "inventory")
            self.chunk = self.source.read(min(self.unread, CHUNK))
            require(bool(self.chunk), "inventory")
            self.unread -= len(self.chunk)
            self.digest.update(self.chunk)
            self.offset = 0

    def take_any(self) -> bytes:
        self.fill()
        self.offset += 1
        return self.chunk[self.offset - 1 : self.offset]

    def take(self, ending: bytes, limit: int) -> bytes:
        result = bytearray()
        while len(result) < limit:
            self.fill()
            end = self.chunk.find(ending, self.offset)
            edge = len(self.chunk) if end == -1 else end + 1
            require(edge - self.offset <= limit - len(result), "inventory")
            result.extend(self.chunk[self.offset : edge])
            self.offset = edge
            if end != -1:
                return bytes(result)
        raise Refusal("inventory")


class Index:
    def __init__(self, *, budget: int = MAX_INDEX) -> None:
        require(
            MAX_ENTRIES * (MAX_PATH + PACK.size + 1 + 8) <= budget <= MAX_INDEX,
            "capacity",
        )
        self.paths = bytearray(MAX_ENTRIES * MAX_PATH)
        self.records = bytearray(MAX_ENTRIES * PACK.size)
        self.found = bytearray(MAX_ENTRIES)
        self.count = self.used = self.examined = self.hashed = 0

    def add(self, path: bytes, size: int, digest: bytes) -> None:
        require(
            self.count < MAX_ENTRIES and self.used + len(path) <= len(self.paths),
            "capacity",
        )
        PACK.pack_into(
            self.records, self.count * PACK.size, self.used, len(path), size, digest
        )
        self.paths[self.used : self.used + len(path)] = path
        self.count += 1
        self.used += len(path)

    def record(self, item: int) -> tuple[int, int, int, bytes]:
        return cast(
            tuple[int, int, int, bytes],
            PACK.unpack_from(self.records, item * PACK.size),
        )

    def path(self, item: int) -> bytes:
        offset, size, _, _ = self.record(item)
        return bytes(self.paths[offset : offset + size])

    def lower(self, path: bytes, start: int, end: int) -> int:
        return start + bisect_left(range(start, end), path, key=self.path)

    def inventory(self, source: BinaryIO, length: int, root: Root) -> tuple[int, int]:
        start = self.count
        stream = InventoryStream(source, length)
        require(stream.take(b"[", 1) == b"[", "inventory")
        previous = b""
        while True:
            item = stream.take(b"}", MAX_PATH + 144)
            match = ROW.fullmatch(item)
            if match is None:
                raise Refusal("inventory")
            path = checked_path(match[1])
            require(path > previous, "inventory")
            self.add(
                path,
                integer(int(match[3]), 0, 2**63 - 1),
                bytes.fromhex(match[2].decode("ascii")),
            )
            previous = path
            delimiter = stream.take_any()
            if delimiter == b"]":
                break
            require(delimiter == b",", "inventory")
        require(stream.unread == 0 and stream.offset == len(stream.chunk), "inventory")
        require(stream.digest.hexdigest() == root.inventory, "inventory")
        for path in root.entries:
            cursor = self.lower(path, start, self.count)
            require(cursor < self.count and self.path(cursor) == path, "inventory")
        prefix = root.stdlib + b"/"
        cursor = self.lower(prefix, start, self.count)
        require(
            cursor < self.count and self.path(cursor).startswith(prefix), "inventory"
        )
        return start, self.count

    def file(self, fd: int, name: bytes, saved: os.stat_result, item: int) -> None:
        _, _, size, expected = self.record(item)
        require(
            not self.found[item] and saved.st_nlink == 1 and saved.st_size == size,
            "file",
        )
        require(size <= MAX_FILE_BYTES - self.hashed, "capacity")
        with os.fdopen(os.open(name, FILE, dir_fd=fd), "rb", buffering=0) as source:
            require(os.fstat(source.fileno()) == saved, "file")
            hashed = hashlib.sha256()
            remaining = size
            while remaining:
                block = source.read(min(remaining, CHUNK))
                require(bool(block), "file")
                remaining -= len(block)
                hashed.update(block)
            require(not source.read(1) and os.fstat(source.fileno()) == saved, "file")
        require(
            hashed.digest() == expected
            and os.stat(name, dir_fd=fd, follow_symlinks=False) == saved,
            "file",
        )
        self.hashed += size
        self.found[item] = 1

    def walk(
        self, root: int, mount: int, bounds: tuple[int, int], deadline_ns: int
    ) -> None:
        start, end = bounds
        pending = array.array("I", [0]) * (2 * MAX_ENTRIES)
        queued = cursor = 1
        while cursor <= queued:
            idx, width = pending[2 * (cursor - 1) : 2 * cursor]
            cursor += 1
            prefix = self.path(idx)[:width] if width else b""
            require(time.monotonic_ns() < deadline_ns, "deadline")
            fd = open_directory(root, prefix, mount)
            try:
                require(time.monotonic_ns() < deadline_ns, "deadline")
                with os.scandir(fd) as entries:
                    for entry in entries:
                        self.examined += 1
                        require(self.examined <= MAX_ENTRIES, "capacity")
                        name = os.fsencode(entry.name)
                        path = checked_path(prefix + b"/" + name if prefix else name)
                        actual = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISDIR(actual.st_mode):
                            seek = path + b"/"
                            item = self.lower(seek, start, end)
                            require(
                                item < end and self.path(item).startswith(seek), "tree"
                            )
                            require(queued < MAX_ENTRIES, "capacity")
                            pending[2 * queued] = item
                            pending[2 * queued + 1] = len(path)
                            queued += 1
                        else:
                            item = self.lower(path, start, end)
                            require(
                                stat.S_ISREG(actual.st_mode)
                                and item < end
                                and self.path(item) == path,
                                "tree",
                            )
                            self.file(fd, name, actual, item)
            finally:
                os.close(fd)
        require(all(self.found[start:end]), "tree")


def mount_id(fd: int) -> int:
    with open(f"/proc/self/fdinfo/{fd}", "rb", buffering=512) as info:
        while line := info.readline(PROC_LINE + 1):
            require(len(line) <= PROC_LINE and line.endswith(b"\n"), "root")
            if line.startswith(b"mnt_id:\t"):
                return int(line[8:])
    raise Refusal("root")


def open_directory(root: int, relative: bytes, mount: int) -> int:
    fd = os.dup(root)
    try:
        for name in relative.split(b"/") if relative else ():
            child = os.open(name, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            require(mount_id(fd) == mount, "root")
        return fd
    except (OSError, Refusal, ValueError):
        os.close(fd)
        raise


def device(value: int) -> str:
    return f"{os.major(value)}:{os.minor(value)}"


def identity(fd: int) -> tuple[int, str, int]:
    entry = os.fstat(fd)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    require(
        stat.S_ISDIR(entry.st_mode)
        and flags & (os.O_ACCMODE | os.O_PATH) == os.O_RDONLY,
        "root",
    )
    return mount_id(fd), device(entry.st_dev), entry.st_ino


def namespace() -> tuple[int, int]:
    entry = os.stat("/proc/self/ns/mnt")
    return entry.st_dev, entry.st_ino


@dataclass(frozen=True)
class Mount:
    number: int
    dev: bytes
    path: bytes
    whole_read_only: bool


def mount_rows() -> Generator[Mount, None, None]:
    with open("/proc/self/mountinfo", "rb", buffering=PROC_LINE) as stream:
        while line := stream.readline(PROC_LINE + 1):
            require(len(line) <= PROC_LINE and line.endswith(b"\n"), "root")
            before, sep, after = line.partition(b" - ")
            lhs, rhs = before.split(None, 6), after.split(None, 3)
            require(bool(sep) and len(lhs) >= 6 and len(rhs) == 3, "root")
            options = b"," + rhs[2] + b","
            yield Mount(
                int(lhs[0]),
                lhs[2],
                lhs[4],
                len(lhs) == 6
                and lhs[3] == b"/"
                and rhs[0] == b"ext4"
                and b",ro," in b"," + lhs[5] + b","
                and b",ro," in options
                and b",norecovery," in options,
            )


def mounts(roots: tuple[tuple[int, str, int], tuple[int, str, int]]) -> None:
    require(roots[0][0] != roots[1][0] and roots[0][1] != roots[1][1], "root")
    paths = tuple(f"{ANCHOR}/roots/{role}".encode() for role in ROLES)
    found: set[int] = set()
    with closing(mount_rows()) as rows:
        for entry in rows:
            for idx, ((number, dev, _), path) in enumerate(
                zip(roots, paths, strict=True)
            ):
                require(not entry.path.startswith(path + b"/"), "root")
                if entry.number != number:
                    require(entry.path != path and entry.dev != dev.encode(), "root")
                    continue
                require(
                    idx not in found
                    and entry.dev == dev.encode()
                    and entry.path == path
                    and entry.whole_read_only,
                    "root",
                )
                found.add(idx)
    require(len(found) == 2, "root")


def frame_size(header: int, sizes: tuple[int, int, int]) -> bytes:
    require(
        0 < header <= MAX_HEADER
        and all(length > 0 for length in sizes)
        and sum(sizes) <= MAX_DOCUMENTS,
        "capacity",
    )
    return FRAME.pack(sum(sizes) + header + 16, header, *sizes)


def exact(source: BinaryIO, length: int) -> bytes:
    raw = source.read(length)
    require(len(raw) == length, "channel")
    return raw


def restricted(fds: tuple[int, int, int, int]) -> None:
    with open("/proc/self/status", "rb") as source:
        status = source.read(PROC_LINE + 1)
    require(len(status) <= PROC_LINE and status.endswith(b"\n"), "child")
    fields = dict(line.split(b":", 1) for line in status.splitlines() if b":" in line)
    user, group = fields[b"Uid"].split(), fields[b"Gid"].split()
    require(user == group == [b"65532"] * 4 and not fields[b"Groups"].strip(), "child")
    for key in (b"CapInh", b"CapPrm", b"CapEff", b"CapBnd", b"CapAmb"):
        require(fields[key].strip() == b"0000000000000000", "child")
    require(fields[b"NoNewPrivs"].strip() == b"1", "child")
    expected = {0, 1, 2, *fds}
    require(len(expected) == 7, "child")
    names: list[str] = []
    with os.scandir("/proc/self/fd") as opened:
        for entry in opened:
            require(len(names) <= len(expected), "child")
            names.append(entry.name)
    for name in names:
        fd = int(name)
        try:
            os.fstat(fd)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            require(fd in expected, "child")


def check_mappings(
    value: object, devices: tuple[str, str], plan: Deployment, job: str
) -> None:
    require(type(value) is list, "verity")
    rows = cast(list[object], value)
    require(len(rows) == 2, "verity")
    seen: set[str] = set()
    for role, dev, root, row in zip(ROLES, devices, plan.roots, rows, strict=True):
        require(type(row) is list, "verity")
        entry = cast(list[object], row)
        require(len(entry) == 4, "verity")
        uuid, mapped, table, status = entry
        require((uuid, mapped, status) == (f"KORA-{job}-{role}", dev, "V"), "verity")
        if not isinstance(table, str):
            raise Refusal("verity")
        parts = table.split()
        geometry = [
            "4096",
            "4096",
            str(root.blocks),
            "0",
            "sha256",
            root.digest,
            root.salt,
        ]
        require(
            len(parts) == 10 and parts[0] == "1" and parts[3:] == geometry, "verity"
        )
        require(parts[1] != parts[2] and not seen.intersection(parts[1:3]), "verity")
        seen.update(parts[1:3])


def child() -> bytes:
    req, result, first, second = (
        int(os.environ[name])
        for name in (
            "KORA_PREP_REQ",
            "KORA_PREP_RESULT",
            "KORA_PREP_VERIFIER",
            "KORA_PREP_CANDIDATE",
        )
    )
    restricted((req, result, first, second))
    deadline = int(os.environ["KORA_PREP_DEADLINE_NS"])
    with os.fdopen(req, "rb", buffering=CHUNK) as source:
        header = exact(source, FRAME.size)
        _, meta_size, manifest_size, size1, size2 = cast(
            tuple[int, int, int, int, int], FRAME.unpack(header)
        )
        require(
            header == frame_size(meta_size, (manifest_size, size1, size2))
            and manifest_size <= MAX_MANIFEST,
            "channel",
        )
        meta = keys(
            decoded(exact(source, meta_size)), "job selector namespace roots mappings"
        )
        selector = hex_digest(meta["selector"])
        job = hex_digest(meta["job"])
        manifest = deployment(exact(source, manifest_size), selector)
        index = Index()
        bounds1 = index.inventory(source, size1, manifest.roots[0])
        bounds2 = index.inventory(source, size2, manifest.roots[1])
        require(not source.read(1), "channel")
        require(time.monotonic_ns() < deadline, "deadline")
    try:
        actual = (identity(first), identity(second))
        require(
            meta["namespace"] == list(namespace())
            and meta["roots"] == [list(item) for item in actual],
            "root",
        )
        require(time.monotonic_ns() < deadline, "deadline")
        check_mappings(meta["mappings"], (actual[0][1], actual[1][1]), manifest, job)
        mounts(actual)
        try:
            index.walk(first, actual[0][0], bounds1, deadline)
            index.walk(second, actual[1][0], bounds2, deadline)
        except OSError as error:
            if error.errno == errno.EIO:
                raise ReadRefusal("read") from error
            raise
    finally:
        os.close(first)
        os.close(second)
    payload = canonical(
        {
            "job": job,
            "selector": selector,
            "entries": index.examined,
            "bytes": index.hashed,
        }
    )
    with os.fdopen(result, "wb", buffering=0) as sink:
        response = struct.pack("!I", len(payload)) + payload
        require(sink.write(response) == len(response), "channel")
    return payload


if __name__ == "__main__":
    try:
        child()
    except ReadRefusal:
        sys.exit(READ_REFUSAL_EXIT)
    except (Refusal, OSError, KeyError, ValueError):
        sys.exit(1)
