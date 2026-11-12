import hashlib
import json
import os
import re
import signal
import stat
import struct
import sys
import unicodedata
from bisect import bisect_left
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import closing, contextmanager
from types import FrameType
from typing import Final, NamedTuple, TypeGuard, cast

KIB: Final[int] = 1024
MIB: Final[int] = 1024 * KIB
MAX_REVISIONS: Final[int] = 1000
MAX_MESSAGE_BYTES: Final[int] = 64 * KIB
MAX_REVISION_BYTES: Final[int] = 80 * KIB
MAX_TREE_ENTRIES: Final[int] = 50_000
MAX_TREE_BYTES: Final[int] = 64 * MIB
MAX_BLOB_BYTES: Final[int] = 256 * MIB
MAX_CHAIN_BYTES: Final[int] = 512 * MIB
MAX_CHAIN_BLOBS: Final[int] = 100_000
MAX_NAMED_BLOBS: Final[int] = 200_000
MAX_PATH_BYTES: Final[int] = 4096
MAX_COMPONENT_BYTES: Final[int] = 255
MAX_SYMLINK_BYTES: Final[int] = 4096
MAX_CONTROL_BYTES: Final[int] = 8192
MAX_TREE_TOKENS: Final[int] = 5 * MAX_TREE_ENTRIES
JSON_ESCAPE: Final[re.Pattern[bytes]] = re.compile(rb"\\.", re.DOTALL)
JSON_STRING: Final[re.Pattern[bytes]] = re.compile(rb'"[^"]*"')
IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
READ_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
DIRECTORY_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
NEW_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
RECORD_HEADER: Final[struct.Struct] = struct.Struct(">cQ")
REVISION_KIND: Final[bytes] = b"R"
TREE_KIND: Final[bytes] = b"T"
BLOB_KIND: Final[bytes] = b"B"
RECORD_BOUNDS: Final[dict[bytes, int]] = {
    REVISION_KIND: MAX_REVISION_BYTES,
    TREE_KIND: MAX_TREE_BYTES,
    BLOB_KIND: MAX_BLOB_BYTES,
}
RECORD_ORDER: Final[tuple[bytes, ...]] = (REVISION_KIND, TREE_KIND, BLOB_KIND)
REGULAR: Final[str] = "regular"
EXECUTABLE: Final[str] = "executable"
SYMLINK: Final[str] = "symlink"
MODES: Final[frozenset[str]] = frozenset({REGULAR, EXECUTABLE, SYMLINK})
ENTRY_KEYS: Final[frozenset[str]] = frozenset({"path", "mode", "size", "sha256"})
REVISION_KEYS: Final[frozenset[str]] = frozenset({"parent", "message", "tree"})
SECTIONS: Final[tuple[str, ...]] = ("revisions", "trees", "blobs")
RESERVED: Final[frozenset[str]] = frozenset({".git", ".kora", "git~1", "kora~1"})
IGNORABLE: Final[frozenset[str]] = frozenset("‌‍‎‏‪‫‬‭‮⁪⁫⁬⁭⁮⁯﻿")
CANDIDATE_STORE: Final[tuple[str, ...]] = (".kora", "dirvcs")
HEAD_NAME: Final[str] = "head"
CHECKOUT_NAME: Final[str] = "checkout.json"
REFUSED: Final[int] = 3
FAILED: Final[int] = 4

MAX_PREPARED_REVISIONS: Final[int] = MAX_REVISIONS + 1
MAX_ANCESTRY: Final[int] = 10_000
MAX_ANCESTRY_TREE_BYTES: Final[int] = 256 * MIB
MAX_INDEX_RECORDS: Final[int] = 100_000
MAX_INDEX_BYTES: Final[int] = 128 * MIB
SEALED_MODES: Final[dict[str, str]] = {
    "100644": REGULAR,
    "100755": EXECUTABLE,
    "120000": SYMLINK,
}
EXCLUDED: Final[frozenset[str]] = frozenset({".git", ".kora"})
PREPARED_STORE: Final[str] = "store"
PREPARED_NAME: Final[str] = "prepared"
INDEX_NAME: Final[str] = "index.jsonl"
PREPARED_DIGEST_NAME: Final[str] = "prepared_digest"
COMMIT_MESSAGE: Final[re.Pattern[str]] = re.compile(
    r'kora: [^\s{}"\\\x00-\x1f]+ #[1-9][0-9]*'
)


class Refused(Exception):
    pass


class Failed(Exception):
    pass


class Entry(NamedTuple):
    path: str
    mode: str
    size: int
    sha256: str


class Revision(NamedTuple):
    identifier: str
    parent: str | None
    message: str
    tree: str
    size: int


class Pin(NamedTuple):
    path: str
    device: int
    inode: int


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_identifier(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def checked_id(value: object) -> str:
    if not is_identifier(value):
        raise Refused
    return value


def canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def refuse_constant(name: str) -> object:
    raise Refused


def canonical_document(data: bytes) -> object:
    try:
        document: object = json.loads(data, parse_constant=refuse_constant)
    except (ValueError, RecursionError):
        raise Refused from None
    if canonical(document) != data:
        raise Refused
    return document


def is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def reserved(component: str) -> bool:
    visible = "".join(item for item in component if item not in IGNORABLE)
    trimmed = visible.split(":", 1)[0].rstrip(". ")
    return unicodedata.normalize("NFC", trimmed).casefold() in RESERVED


def path_allowed(path: str) -> bool:
    if not path or path.startswith("/") or len(path.encode("utf-8")) > MAX_PATH_BYTES:
        return False
    if any(ord(item) < 0x20 or ord(item) == 0x7F for item in path):
        return False
    return all(
        part not in ("", ".", "..")
        and len(part.encode("utf-8")) <= MAX_COMPONENT_BYTES
        and not reserved(part)
        for part in path.split("/")
    )


def folded(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def check_tree(entries: Sequence[Entry]) -> None:
    if len(entries) > MAX_TREE_ENTRIES:
        raise Refused
    paths: set[str] = set()
    for entry in entries:
        if not path_allowed(entry.path):
            raise Refused
        key = folded(entry.path)
        if key in paths:
            raise Refused
        paths.add(key)
    ordered = sorted(paths)
    for key in ordered:
        prefix = key + "/"
        index = bisect_left(ordered, prefix)
        if index < len(ordered) and ordered[index].startswith(prefix):
            raise Refused


def entry_of(item: object) -> Entry:
    if not isinstance(item, dict):
        raise Refused
    fields = cast("dict[str, object]", item)
    if fields.keys() != ENTRY_KEYS:
        raise Refused
    path, mode, size = fields["path"], fields["mode"], fields["size"]
    if not isinstance(path, str) or not isinstance(mode, str) or mode not in MODES:
        raise Refused
    limit = MAX_SYMLINK_BYTES if mode == SYMLINK else MAX_BLOB_BYTES
    if not is_count(size) or size > limit:
        raise Refused
    return Entry(path, mode, size, checked_id(fields["sha256"]))


def tree_of(data: bytes) -> tuple[Entry, ...]:
    if len(data) > MAX_TREE_BYTES:
        raise Refused
    structure = JSON_STRING.sub(b"", JSON_ESCAPE.sub(b"", data))
    if structure.count(b"{") > MAX_TREE_ENTRIES:
        raise Refused
    if structure.count(b",") + structure.count(b"[") > MAX_TREE_TOKENS:
        raise Refused
    document = canonical_document(data)
    if not isinstance(document, list):
        raise Refused
    entries = tuple(entry_of(item) for item in cast("list[object]", document))
    if [entry.path for entry in entries] != sorted({entry.path for entry in entries}):
        raise Refused
    check_tree(entries)
    return entries


def tree_document(entries: Sequence[Entry]) -> bytes:
    ordered = sorted(entries, key=lambda entry: entry.path)
    return canonical(
        [
            {
                "path": entry.path,
                "mode": entry.mode,
                "size": entry.size,
                "sha256": entry.sha256,
            }
            for entry in ordered
        ]
    )


def is_message(message: object) -> TypeGuard[str]:
    return (
        isinstance(message, str)
        and len(message.encode("utf-8")) <= MAX_MESSAGE_BYTES
        and json.dumps(message, ensure_ascii=False) == f'"{message}"'
    )


def revision_of(data: bytes) -> Revision:
    if len(data) > MAX_REVISION_BYTES:
        raise Refused
    document = canonical_document(data)
    if not isinstance(document, dict):
        raise Refused
    fields = cast("dict[str, object]", document)
    if fields.keys() != REVISION_KEYS:
        raise Refused
    parent, message = fields["parent"], fields["message"]
    if not is_message(message):
        raise Refused
    return Revision(
        identifier=digest_of(data),
        parent=None if parent is None else checked_id(parent),
        message=message,
        tree=checked_id(fields["tree"]),
        size=len(data),
    )


def revision_document(parent: str | None, message: str, tree: str) -> bytes:
    return canonical({"parent": parent, "message": message, "tree": tree})


def open_directory(parent: int | None, name: str) -> int:
    if parent is None:
        return os.open(name, DIRECTORY_FLAGS)
    return os.open(name, DIRECTORY_FLAGS, dir_fd=parent)


def make_directory(parent: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return open_directory(parent, name)


def read_limited(
    directory: int,
    name: str,
    limit: int,
    stopped: Callable[[], bool] = lambda: False,
) -> bytes:
    descriptor = os.open(name, READ_FLAGS, dir_fd=directory)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
            raise Refused
        chunks: list[bytes] = []
        size = 0
        while not stopped():
            block = os.read(descriptor, min(MIB, limit + 1 - size))
            size += len(block)
            if size > limit:
                raise Refused
            if not block:
                return b"".join(chunks)
            chunks.append(block)
        raise Failed
    finally:
        os.close(descriptor)


def write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def pump(
    source: int, target: int | None, limit: int, exact: int | None = None
) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    while exact is None or size < exact:
        block = os.read(source, MIB if exact is None else min(MIB, exact - size))
        if not block:
            if exact is not None:
                raise Refused
            break
        size += len(block)
        if size > limit:
            raise Refused
        hasher.update(block)
        if target is not None:
            write_all(target, block)
    return hasher.hexdigest(), size


def open_regular(directory: int, name: str) -> int:
    descriptor = os.open(name, READ_FLAGS, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise Refused
    return descriptor


def stream_digest(directory: int, name: str, limit: int) -> str:
    descriptor = open_regular(directory, name)
    try:
        return pump(descriptor, None, limit)[0]
    finally:
        os.close(descriptor)


def same_content(directory: int, name: str, digest: str, limit: int) -> bool:
    try:
        return stream_digest(directory, name, limit) == digest
    except (OSError, Refused):
        return False


class Scratch:
    def __init__(self, directory: int, name: str) -> None:
        self.directory = directory
        self.name = name
        self.descriptor = -1

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, -1
        if descriptor >= 0:
            os.close(descriptor)


SCRATCH: Final[set[Scratch]] = set()


def scratch_file(directory: int) -> Scratch:
    scratch = Scratch(directory, f".tmp-{os.urandom(8).hex()}")
    SCRATCH.add(scratch)
    scratch.descriptor = os.open(scratch.name, NEW_FLAGS, 0o600, dir_fd=directory)
    return scratch


def discard(scratch: Scratch) -> None:
    scratch.close()
    try:
        os.unlink(scratch.name, dir_fd=scratch.directory)
    except FileNotFoundError:
        pass
    SCRATCH.discard(scratch)


def discard_all() -> None:
    for scratch in list(SCRATCH):
        discard(scratch)


def settle(
    scratch: Scratch,
    directory: int,
    name: str,
    digest: str | None,
    durable: bool = False,
) -> None:
    if durable:
        os.fsync(scratch.descriptor)
    scratch.close()
    try:
        os.link(
            scratch.name,
            name,
            src_dir_fd=scratch.directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
    except FileExistsError:
        if digest is None or not same_content(directory, name, digest, MAX_BLOB_BYTES):
            raise Failed from None
    finally:
        discard(scratch)


def put_bytes(directory: int, name: str, data: bytes) -> None:
    scratch = scratch_file(directory)
    write_all(scratch.descriptor, data)
    settle(scratch, directory, name, digest_of(data))


class Store:
    def __init__(self, root: int, create: bool = False) -> None:
        self.root = root
        opener = make_directory if create else open_directory
        self.sections: dict[str, int] = {}
        try:
            for section in SECTIONS:
                self.sections[section] = opener(root, section)
        except OSError:
            self.close()
            raise

    def close(self) -> None:
        for descriptor in self.sections.values():
            os.close(descriptor)
        self.sections.clear()

    @property
    def revisions(self) -> int:
        return self.sections["revisions"]

    @property
    def trees(self) -> int:
        return self.sections["trees"]

    @property
    def blobs(self) -> int:
        return self.sections["blobs"]

    def revision(self, identifier: str) -> Revision:
        revision = revision_of(
            read_limited(self.revisions, checked_id(identifier), MAX_REVISION_BYTES)
        )
        if revision.identifier != identifier:
            raise Refused
        return revision

    def tree_bytes(self, identifier: str) -> bytes:
        data = read_limited(self.trees, checked_id(identifier), MAX_TREE_BYTES)
        if digest_of(data) != identifier:
            raise Refused
        return data

    def tree(self, identifier: str) -> tuple[Entry, ...]:
        return tree_of(self.tree_bytes(identifier))

    def has(self, section: int, identifier: str) -> bool:
        try:
            status = os.stat(identifier, dir_fd=section, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return stat.S_ISREG(status.st_mode)


def changes(
    before: Sequence[Entry], after: Sequence[Entry]
) -> tuple[list[Entry], set[str]]:
    previous = {entry.path: entry for entry in before}
    introduced = [
        entry
        for entry in after
        if (old := previous.get(entry.path)) is None
        or (old.mode, old.sha256) != (entry.mode, entry.sha256)
    ]
    current = {entry.path for entry in after}
    paths = {entry.path for entry in introduced} | (previous.keys() - current)
    return introduced, paths


def read_pin(path: str) -> Pin:
    descriptor = os.open(path, READ_FLAGS)
    try:
        data = os.read(descriptor, MAX_CONTROL_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_CONTROL_BYTES:
        raise Failed
    document: object = json.loads(data)
    if not isinstance(document, dict):
        raise Failed
    fields = cast("dict[str, object]", document)
    path_value, device, inode = (
        fields.get("path"),
        fields.get("device"),
        fields.get("inode"),
    )
    if not isinstance(path_value, str) or not is_count(device) or not is_count(inode):
        raise Failed
    return Pin(path_value, device, inode)


def open_pinned(pin: Pin) -> int:
    descriptor = os.open(pin.path, DIRECTORY_FLAGS)
    status = os.fstat(descriptor)
    if (status.st_dev, status.st_ino) != (pin.device, pin.inode):
        os.close(descriptor)
        raise Failed
    return descriptor


def open_repository(control: str) -> int:
    return open_pinned(read_pin(control))


def read_head(pin: Pin, branch: str) -> str:
    root = open_pinned(pin)
    try:
        current = open_directory(root, "heads")
        try:
            for part in branch.split("/")[:-1]:
                following = open_directory(current, part)
                os.close(current)
                current = following
            data = read_limited(current, branch.split("/")[-1], MAX_CONTROL_BYTES)
        finally:
            os.close(current)
    finally:
        os.close(root)
    return checked_id(data.decode("ascii"))


def is_branch(name: str) -> bool:
    if not name or len(name.encode("utf-8")) > MAX_COMPONENT_BYTES:
        return False
    return all(
        part
        and part not in (".", "..")
        and not part.startswith((".", "-"))
        and not any(
            ord(item) < 0x20 or ord(item) == 0x7F or item in "{}" for item in part
        )
        for part in name.split("/")
    )


def emit(text: str) -> None:
    sys.stdout.write(text)


class Opened:
    def __init__(self) -> None:
        self.descriptors: list[int] = []
        self.stores: list[Store] = []

    def directory(self, parent: int | None, name: str, create: bool = False) -> int:
        if create and parent is not None:
            descriptor = make_directory(parent, name)
        else:
            descriptor = open_directory(parent, name)
        self.descriptors.append(descriptor)
        return descriptor

    def repository(self, control: str) -> Store:
        root = open_repository(control)
        self.descriptors.append(root)
        return self.store(root)

    def store(self, root: int, create: bool = False) -> Store:
        opened = Store(root, create)
        self.stores.append(opened)
        return opened

    def close(self) -> None:
        discard_all()
        for store in self.stores:
            store.close()
        for descriptor in self.descriptors:
            os.close(descriptor)


def check_repository(control: str) -> None:
    opened = Opened()
    try:
        opened.directory(opened.repository(control).root, "heads")
    finally:
        opened.close()


def verify(control: str, revisions: Sequence[str]) -> None:
    opened = Opened()
    try:
        repository = opened.repository(control)
        for identifier in revisions:
            repository.tree(repository.revision(identifier).tree)
    finally:
        opened.close()


def descend(root: int, parts: Sequence[str], opened: Opened) -> int:
    current = root
    for part in parts:
        current = opened.directory(current, part, create=True)
    return current


def made_parent(root: int, parts: Sequence[str]) -> int:
    current = os.dup(root)
    try:
        for part in parts:
            following = make_directory(current, part)
            os.close(current)
            current = following
        return os.dup(current)
    finally:
        os.close(current)


def materialise_entry(blobs: int, root: int, entry: Entry) -> None:
    parts = entry.path.split("/")
    parent = made_parent(root, parts[:-1])
    try:
        if entry.mode != SYMLINK:
            materialise_file(blobs, entry, parent, parts[-1])
            return
        target = read_limited(blobs, entry.sha256, MAX_SYMLINK_BYTES)
        if len(target) != entry.size or digest_of(target) != entry.sha256:
            raise Failed
        os.symlink(target, parts[-1], dir_fd=parent)
    finally:
        os.close(parent)


def materialise(control: str, revision: str) -> None:
    opened = Opened()
    try:
        repository = opened.repository(control)
        checkout = repository.revision(revision)
        tree_data = repository.tree_bytes(checkout.tree)
        entries = tree_of(tree_data)
        root = opened.directory(None, ".")
        for entry in sorted(entries, key=lambda item: item.mode == SYMLINK):
            materialise_entry(repository.blobs, root, entry)
        store_root = descend(root, CANDIDATE_STORE, opened)
        candidate = opened.store(store_root, create=True)
        put_bytes(candidate.trees, checkout.tree, tree_data)
        put_bytes(
            store_root,
            CHECKOUT_NAME,
            canonical({"revision": revision, "tree": checkout.tree}),
        )
        put_bytes(store_root, HEAD_NAME, revision.encode("ascii"))
    finally:
        opened.close()


def materialise_file(blobs: int, entry: Entry, parent: int, name: str) -> None:
    source = open_regular(blobs, entry.sha256)
    try:
        mode = 0o755 if entry.mode == EXECUTABLE else 0o644
        target = os.open(name, NEW_FLAGS, mode, dir_fd=parent)
        try:
            digest, _ = pump(source, target, entry.size, entry.size)
            if os.read(source, 1):
                raise Refused
            os.fchmod(target, mode)
        finally:
            os.close(target)
    finally:
        os.close(source)
    if digest != entry.sha256:
        raise Failed


def changed(control: str, base: str, revision: str) -> None:
    opened = Opened()
    try:
        paths = changed_between(opened.repository(control), base, revision)
    finally:
        opened.close()
    emit(json.dumps(sorted(paths), ensure_ascii=False))


def changed_between(store: Store, base: str, revision: str) -> set[str]:
    before = store.tree(store.revision(base).tree)
    after = store.tree(store.revision(revision).tree)
    return changes(before, after)[1]


def ancestry(
    store: Store, base: str, cancelled: Callable[[], bool] = lambda: False
) -> list[Revision]:
    found: list[Revision] = []
    total = 0
    current: str | None = base
    while current is not None:
        if cancelled():
            raise Failed
        revision = store.revision(current)
        found.append(revision)
        total += os.stat(
            revision.tree, dir_fd=store.trees, follow_symlinks=False
        ).st_size
        if len(found) > MAX_ANCESTRY or total > MAX_ANCESTRY_TREE_BYTES:
            raise Refused
        current = revision.parent
    return found


def new_revisions(
    store: Store,
    head: str,
    base: str,
    limit: int,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[Revision]:
    found: list[Revision] = []
    current: str | None = head
    while current != base:
        if cancelled():
            raise Failed
        if current is None or len(found) >= limit:
            raise Refused
        revision = store.revision(current)
        found.append(revision)
        current = revision.parent
    found.reverse()
    return found


def base_sizes(
    store: Store,
    lineage: Sequence[Revision],
    wanted: set[str],
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, int]:
    held: dict[str, int] = {}
    seen: set[str] = set()
    for revision in lineage:
        if cancelled():
            raise Failed
        if revision.tree in seen:
            continue
        seen.add(revision.tree)
        for entry in store.tree(revision.tree):
            if entry.sha256 in wanted:
                held.setdefault(entry.sha256, entry.size)
    return held


def history(
    store: Store, base: str, prepared: str
) -> Iterator[tuple[Revision, list[Entry], set[str]]]:
    before = store.tree(store.revision(base).tree)
    for revision in new_revisions(store, prepared, base, MAX_PREPARED_REVISIONS):
        after = store.tree(revision.tree)
        introduced, paths = changes(before, after)
        yield revision, introduced, paths
        before = after


def new_objects(
    store: Store,
    base: str,
    prepared: str,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[list[Revision], set[str]]:
    revisions = new_revisions(store, prepared, base, MAX_PREPARED_REVISIONS, cancelled)
    named: set[str] = set()
    for tree in dict.fromkeys(revision.tree for revision in revisions):
        if cancelled():
            raise Failed
        named.update(entry.sha256 for entry in store.tree(tree))
    held = base_sizes(store, ancestry(store, base, cancelled), named, cancelled)
    return revisions, named - held.keys()


def read_text_file(directory: int, name: str) -> str:
    return read_limited(directory, name, MAX_CONTROL_BYTES).decode("ascii").strip()


def working_files(root: int) -> Iterator[str]:
    pending = [""]
    visited = 0
    previous = ""
    directory = os.dup(root)
    try:
        while pending:
            prefix = pending.pop()
            relative = prefix
            if previous and prefix.startswith(previous + "/"):
                relative = prefix[len(previous) + 1 :]
            else:
                os.close(directory)
                directory = -1
                directory = os.dup(root)
            for component in relative.split("/") if relative else ():
                following = open_directory(directory, component)
                os.close(directory)
                directory = following
            previous = prefix
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not prefix and entry.name in EXCLUDED:
                        continue
                    path = f"{prefix}/{entry.name}" if prefix else entry.name
                    visited += 1
                    if visited > MAX_TREE_ENTRIES or not path_allowed(path):
                        raise Refused
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    else:
                        yield path
    finally:
        if directory >= 0:
            os.close(directory)


def stored_blob(root: int, blobs: int, path: str, known: set[str]) -> Entry:
    *directories, name = path.split("/")
    parent = made_parent(root, directories)
    try:
        status = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(status.st_mode):
            target = os.readlink(name, dir_fd=parent).encode("utf-8", "surrogateescape")
            sha = digest_of(target)
            if sha not in known:
                put_bytes(blobs, sha, target)
            return Entry(path, SYMLINK, len(target), sha)
        descriptor = open_regular(parent, name)
    finally:
        os.close(parent)
    try:
        scratch = scratch_file(blobs)
        sha, size = pump(descriptor, scratch.descriptor, MAX_BLOB_BYTES)
    finally:
        os.close(descriptor)
    if sha in known:
        discard(scratch)
    else:
        settle(scratch, blobs, sha, sha)
    mode = EXECUTABLE if status.st_mode & stat.S_IXUSR else REGULAR
    return Entry(path, mode, size, sha)


def commit(message: str) -> None:
    if not is_message(message):
        raise Refused
    opened = Opened()
    try:
        root = opened.directory(None, ".")
        store_root = descend(root, CANDIDATE_STORE, opened)
        candidate = opened.store(store_root)
        head = checked_id(read_text_file(store_root, HEAD_NAME))
        known = {
            entry.sha256 for entry in candidate.tree(checkout_record(store_root)[1])
        }
        entries = [
            stored_blob(root, candidate.blobs, path, known)
            for path in working_files(root)
        ]
        tree_data = tree_document(entries)
        tree = digest_of(tree_data)
        put_bytes(candidate.trees, tree, tree_data)
        document = revision_document(head, message, tree)
        identifier = digest_of(document)
        put_bytes(candidate.revisions, identifier, document)
        os.unlink(HEAD_NAME, dir_fd=store_root)
        put_bytes(store_root, HEAD_NAME, identifier.encode("ascii"))
    finally:
        opened.close()
    emit(identifier)


def checkout_record(store_root: int) -> tuple[str, str]:
    document: object = json.loads(
        read_limited(store_root, CHECKOUT_NAME, MAX_CONTROL_BYTES)
    )
    if not isinstance(document, dict):
        raise Refused
    fields = cast("dict[str, object]", document)
    return checked_id(fields.get("revision")), checked_id(fields.get("tree"))


def record(descriptor: int, kind: bytes, data: bytes) -> None:
    write_all(descriptor, RECORD_HEADER.pack(kind, len(data)))
    write_all(descriptor, data)


def blob_record(descriptor: int, blobs: int, sha: str) -> None:
    source = open_regular(blobs, sha)
    try:
        size = os.fstat(source).st_size
        write_all(descriptor, RECORD_HEADER.pack(BLOB_KIND, size))
        pump(source, descriptor, size, size)
    finally:
        os.close(source)


def write_chain(
    candidate: Store, head: str, checkout: str, checkout_tree: str, output: int
) -> None:
    revisions = new_revisions(candidate, head, checkout, MAX_REVISIONS)
    known = {entry.sha256 for entry in candidate.tree(checkout_tree)}
    trees = list(
        dict.fromkeys(
            revision.tree for revision in revisions if revision.tree != checkout_tree
        )
    )
    scratch = scratch_file(output)
    for revision in revisions:
        record(
            scratch.descriptor,
            REVISION_KIND,
            read_limited(candidate.revisions, revision.identifier, MAX_REVISION_BYTES),
        )
    blobs: list[str] = []
    for tree in trees:
        data = candidate.tree_bytes(tree)
        record(scratch.descriptor, TREE_KIND, data)
        for entry in tree_of(data):
            if entry.sha256 not in known:
                known.add(entry.sha256)
                blobs.append(entry.sha256)
    for sha in blobs:
        blob_record(scratch.descriptor, candidate.blobs, sha)
    settle(scratch, output, "head.bundle", None)


def fresh_output(root: int, path: str, opened: Opened) -> int:
    parts = path.split("/")
    parent = descend(root, parts[:-1], opened)
    try:
        status = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return opened.directory(parent, parts[-1], create=True)
    if not stat.S_ISDIR(status.st_mode):
        os.unlink(parts[-1], dir_fd=parent)
    else:
        output = opened.directory(parent, parts[-1])
        with os.scandir(output) as entries:
            for entry in entries:
                os.unlink(entry.name, dir_fd=output)
        return output
    return opened.directory(parent, parts[-1], create=True)


def export(checkout: str, out: str) -> None:
    opened = Opened()
    try:
        root = opened.directory(None, ".")
        store_root = descend(root, CANDIDATE_STORE, opened)
        candidate = opened.store(store_root)
        head = checked_id(read_text_file(store_root, HEAD_NAME))
        recorded, checkout_tree = checkout_record(store_root)
        if recorded != checkout:
            raise Refused
        output = fresh_output(root, out, opened)
        bundle = head != checkout
        if bundle:
            write_chain(candidate, head, checkout, checkout_tree, output)
        scratch = scratch_file(output)
        size = 0
        for count, path in enumerate(working_files(root), 1):
            data = (
                json.dumps({"path": path, "gitlink": None}, ensure_ascii=False) + "\n"
            ).encode("utf-8")
            size += len(data)
            if count > MAX_TREE_ENTRIES or size > MAX_TREE_BYTES:
                raise Refused
            write_all(scratch.descriptor, data)
        settle(scratch, output, "manifest.jsonl", None)
        put_bytes(
            output,
            "head.json",
            json.dumps({"head": head, "bundle": bundle}).encode("ascii"),
        )
    finally:
        opened.close()


def check_ancestry(control: str, base: str) -> None:
    opened = Opened()
    try:
        ancestry(opened.repository(control), base)
    finally:
        opened.close()


def copy_base(control: str, base: str) -> None:
    opened = Opened()
    try:
        repository = opened.repository(control)
        root = opened.directory(None, ".")
        prepared = opened.store(
            opened.directory(root, PREPARED_STORE, create=True), create=True
        )
        for revision in ancestry(repository, base):
            copy_object(
                repository.revisions,
                prepared.revisions,
                revision.identifier,
                MAX_REVISION_BYTES,
            )
            if not prepared.has(prepared.trees, revision.tree):
                copy_object(
                    repository.trees, prepared.trees, revision.tree, MAX_TREE_BYTES
                )
    finally:
        opened.close()


def read_head_document(sealed: int) -> tuple[str, bool]:
    document: object = json.loads(read_limited(sealed, "head.json", MAX_CONTROL_BYTES))
    if not isinstance(document, dict):
        raise Refused
    fields = cast("dict[str, object]", document)
    bundle = fields.get("bundle")
    if not isinstance(bundle, bool):
        raise Refused
    return checked_id(fields.get("head")), bundle


class Chain:
    def __init__(self) -> None:
        self.revisions: list[Revision] = []
        self.revision_files: dict[str, Scratch] = {}
        self.named_trees: set[str] = set()
        self.trees: dict[str, Scratch] = {}
        self.blobs: dict[str, Scratch] = {}
        self.named: dict[str, int] = {}


class ChainReader:
    def __init__(self, descriptor: int, size: int, staging: int) -> None:
        self.descriptor = descriptor
        self.remaining = size
        self.staging = staging
        self.section = REVISION_KIND

    def header(self) -> tuple[bytes, int] | None:
        if self.remaining == 0:
            return None
        if self.remaining < RECORD_HEADER.size:
            raise Refused
        data = os.read(self.descriptor, RECORD_HEADER.size)
        if len(data) != RECORD_HEADER.size:
            raise Refused
        self.remaining -= RECORD_HEADER.size
        kind, size = cast("tuple[bytes, int]", RECORD_HEADER.unpack(data))
        bound = RECORD_BOUNDS.get(kind)
        if bound is None or size > bound or size > self.remaining:
            raise Refused
        if RECORD_ORDER.index(kind) < RECORD_ORDER.index(self.section):
            raise Refused
        self.section = kind
        return kind, size

    def staged(self, size: int) -> tuple[Scratch, str]:
        scratch = scratch_file(self.staging)
        digest, _ = pump(self.descriptor, scratch.descriptor, size, size)
        scratch.close()
        self.remaining -= size
        return scratch, digest


def add_revision(chain: Chain, scratch: Scratch, base: str) -> None:
    revision = revision_of(
        read_limited(scratch.directory, scratch.name, MAX_REVISION_BYTES)
    )
    expected = chain.revisions[-1].identifier if chain.revisions else base
    if revision.parent != expected or len(chain.revisions) >= MAX_REVISIONS:
        raise Refused
    chain.revisions.append(revision)
    chain.named_trees.add(revision.tree)
    chain.revision_files[revision.identifier] = scratch


def add_tree(chain: Chain, scratch: Scratch, tree: str) -> None:
    if tree in chain.trees or tree not in chain.named_trees:
        raise Refused
    chain.trees[tree] = scratch
    for entry in tree_of(read_limited(scratch.directory, scratch.name, MAX_TREE_BYTES)):
        if chain.named.setdefault(entry.sha256, entry.size) != entry.size:
            raise Refused
    if len(chain.named) > MAX_NAMED_BLOBS:
        raise Refused


def add_blob(chain: Chain, scratch: Scratch, sha: str, size: int) -> None:
    if sha in chain.blobs or chain.named.get(sha) != size:
        raise Refused
    if len(chain.blobs) >= MAX_CHAIN_BLOBS:
        raise Refused
    chain.blobs[sha] = scratch


def read_chain(reader: ChainReader, base: str) -> Generator[Scratch, None, Chain]:
    chain = Chain()
    while (header := reader.header()) is not None:
        kind, size = header
        scratch, digest = reader.staged(size)
        yield scratch
        if kind == REVISION_KIND:
            add_revision(chain, scratch, base)
        elif kind == TREE_KIND:
            add_tree(chain, scratch, digest)
        else:
            add_blob(chain, scratch, digest, size)
    return chain


def check_chain(chain: Chain, head: str, prepared: Store, base: str) -> set[str]:
    if not chain.revisions or chain.revisions[-1].identifier != head:
        raise Refused
    if any(
        not prepared.has(prepared.trees, tree)
        for tree in chain.named_trees - chain.trees.keys()
    ):
        raise Refused
    held = base_sizes(prepared, ancestry(prepared, base), set(chain.named))
    for sha, size in chain.named.items():
        if sha in held:
            if held[sha] != size:
                raise Refused
        elif sha not in chain.blobs:
            raise Refused
    return set(held)


def open_chain(sealed: int) -> tuple[int, int]:
    descriptor = open_regular(sealed, "head.bundle")
    size = os.fstat(descriptor).st_size
    if size > MAX_CHAIN_BYTES:
        os.close(descriptor)
        raise Refused
    return descriptor, size


def settle_chain(chain: Chain, prepared: Store, held: set[str]) -> None:
    for tree, scratch in chain.trees.items():
        settle(scratch, prepared.trees, tree, tree)
    for sha, scratch in chain.blobs.items():
        if sha not in held:
            settle(scratch, prepared.blobs, sha, sha)
    for revision in chain.revisions:
        scratch = chain.revision_files[revision.identifier]
        settle(scratch, prepared.revisions, revision.identifier, revision.identifier)


def staged_chain(
    opened: Opened, sealed: int, prepared: Store, head: str, base: str
) -> Generator[Scratch, None, None]:
    descriptor, size = open_chain(sealed)
    opened.descriptors.append(descriptor)
    name = f"incoming-{os.urandom(8).hex()}"
    try:
        staging = opened.directory(prepared.root, name, create=True)
        chain = yield from read_chain(ChainReader(descriptor, size, staging), base)
        settle_chain(chain, prepared, check_chain(chain, head, prepared, base))
    finally:
        discard_all()
        try:
            os.rmdir(name, dir_fd=prepared.root)
        except FileNotFoundError:
            pass


def importation(input_location: str, base: str) -> Generator[Scratch, None, None]:
    opened = Opened()
    try:
        sealed = opened.directory(None, input_location)
        head, bundle = read_head_document(sealed)
        if bundle == (head == base):
            raise Refused
        prepared = opened_prepared(opened)
        if bundle:
            yield from staged_chain(opened, sealed, prepared, head, base)
        put_bytes(prepared.root, HEAD_NAME, head.encode("ascii"))
    finally:
        opened.close()


def import_chain(input_location: str, base: str) -> None:
    with closing(importation(input_location, base)) as stages:
        for _ in stages:
            continue


class SealedRecord(NamedTuple):
    entry: Entry
    chunk: int
    offset: int


def sealed_record(line: bytes) -> SealedRecord:
    document: object = json.loads(line)
    if not isinstance(document, dict):
        raise Refused
    fields = cast("dict[str, object]", document)
    path, mode = fields.get("path"), SEALED_MODES.get(str(fields.get("mode")))
    size, chunk, offset = fields.get("size"), fields.get("chunk"), fields.get("offset")
    if not isinstance(path, str) or mode is None or fields.get("object") is not None:
        raise Refused
    if not (is_count(size) and is_count(chunk) and is_count(offset)):
        raise Refused
    digest = fields.get("sha256")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise Refused
    entry = Entry(path, mode, size, checked_id(digest[len("sha256:") :]))
    if entry.size > (MAX_SYMLINK_BYTES if mode == SYMLINK else MAX_BLOB_BYTES):
        raise Refused
    return SealedRecord(entry, chunk, offset)


def sealed_records(sealed: int) -> list[SealedRecord]:
    descriptor = open_regular(sealed, "tree.jsonl")
    records: list[SealedRecord] = []
    with os.fdopen(descriptor, "rb") as stream:
        size = 0
        while line := stream.readline(8 * MAX_PATH_BYTES + 1):
            size += len(line)
            if (
                len(line) > 8 * MAX_PATH_BYTES
                or size > MAX_TREE_BYTES
                or len(records) >= MAX_TREE_ENTRIES
            ):
                raise Refused
            records.append(sealed_record(line))
    check_tree([item.entry for item in records])
    return records


def import_sealed(
    sealed: int, item: SealedRecord, blobs: int, present: set[str]
) -> None:
    entry = item.entry
    chunk = open_regular(sealed, f"tree.{item.chunk}.bin")
    try:
        scratch = None if entry.sha256 in present else scratch_file(blobs)
        os.lseek(chunk, item.offset, os.SEEK_SET)
        target = None if scratch is None else scratch.descriptor
        digest, _ = pump(chunk, target, entry.size, entry.size)
    finally:
        os.close(chunk)
    if digest != entry.sha256:
        raise Refused
    if scratch is not None:
        settle(scratch, blobs, digest, digest)
        present.add(digest)


def build(input_location: str, base: str, message: str) -> None:
    if COMMIT_MESSAGE.fullmatch(message) is None:
        raise Refused
    opened = Opened()
    try:
        root = opened.directory(None, ".")
        store_root = opened.directory(root, PREPARED_STORE)
        prepared = opened.store(store_root)
        sealed = opened.directory(None, input_location)
        head = checked_id(read_text_file(store_root, HEAD_NAME))
        head_tree = prepared.revision(head).tree
        records = sealed_records(sealed)
        wanted = {item.entry.sha256 for item in records}
        present = set(base_sizes(prepared, ancestry(prepared, base), wanted))
        present.update(sha for sha in wanted if prepared.has(prepared.blobs, sha))
        for item in records:
            import_sealed(sealed, item, prepared.blobs, present)
        tree_data = tree_document([item.entry for item in records])
        if len(tree_data) > MAX_TREE_BYTES:
            raise Refused
        tree = digest_of(tree_data)
        result = head
        if tree != head_tree:
            put_bytes(prepared.trees, tree, tree_data)
            document = revision_document(head, message, tree)
            result = digest_of(document)
            put_bytes(prepared.revisions, result, document)
        if result == base:
            raise Refused
        put_bytes(store_root, PREPARED_NAME, result.encode("ascii"))
    finally:
        opened.close()


def prepared_head() -> None:
    opened = Opened()
    try:
        store_root = opened.directory(opened.directory(None, "."), PREPARED_STORE)
        emit(checked_id(read_text_file(store_root, PREPARED_NAME)))
    finally:
        opened.close()


def opened_prepared(opened: Opened) -> Store:
    return opened.store(opened.directory(opened.directory(None, "."), PREPARED_STORE))


def index_line(
    revision: str, path: str | None, kind: str, location: str, size: int
) -> str:
    return json.dumps(
        {
            "revision": revision,
            "path": path,
            "kind": kind,
            "content_location": location,
            "content_digest": "sha256:" + location,
            "size_bytes": size,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def index_records(store: Store, base: str, prepared: str) -> Iterator[str]:
    _, fresh = new_objects(store, base, prepared)
    seen: set[tuple[str, str]] = set()
    for revision, introduced, _ in history(store, base, prepared):
        yield index_line(
            revision.identifier,
            None,
            "revision_metadata",
            revision.identifier,
            revision.size,
        )
        for entry in introduced:
            key = (entry.sha256, entry.path)
            if entry.sha256 not in fresh or key in seen:
                continue
            seen.add(key)
            kind = "symlink" if entry.mode == SYMLINK else "file"
            yield index_line(
                revision.identifier, entry.path, kind, entry.sha256, entry.size
            )


def index_data(store: Store, base: str, prepared: str, limit: int) -> bytes:
    lines: list[bytes] = []
    size = 0
    for line in index_records(store, base, prepared):
        data = (line + "\n").encode("utf-8")
        size += len(data)
        if size > limit or len(lines) >= MAX_INDEX_RECORDS:
            raise Refused
        lines.append(data)
    return b"".join(lines)


def decode(base: str, prepared: str) -> None:
    opened = Opened()
    try:
        root = opened.directory(None, ".")
        data = index_data(opened_prepared(opened), base, prepared, MAX_INDEX_BYTES)
        put_bytes(root, INDEX_NAME, data)
        prepared_digest = read_text_file(root, PREPARED_DIGEST_NAME)
    finally:
        opened.close()
    manifest = {
        "prepared_digest": prepared_digest,
        "revision": prepared,
        "content_index_location": INDEX_NAME,
        "content_index_digest": "sha256:" + digest_of(data),
        "complete": True,
    }
    emit(json.dumps(manifest, sort_keys=True))


def paths(base: str, prepared: str) -> None:
    opened = Opened()
    found: set[str] = set()
    try:
        for _, _, changed_paths in history(opened_prepared(opened), base, prepared):
            found.update(changed_paths)
    finally:
        opened.close()
    emit(json.dumps(sorted(found), ensure_ascii=False))


def diff_stat(base: str, prepared: str) -> None:
    opened = Opened()
    try:
        changed_paths = changed_between(opened_prepared(opened), base, prepared)
    finally:
        opened.close()
    count = len(changed_paths)
    emit(f" {count} file{'' if count == 1 else 's'} changed")


def publish_objects(
    prepared: Store, repository: Store, base: str, head: str
) -> Generator[Scratch, None, None]:
    revisions, fresh = new_objects(prepared, base, head)
    objects = [
        (prepared.blobs, repository.blobs, sha, MAX_BLOB_BYTES) for sha in sorted(fresh)
    ]
    objects.extend(
        (prepared.trees, repository.trees, tree, MAX_TREE_BYTES)
        for tree in dict.fromkeys(revision.tree for revision in revisions)
    )
    objects.extend(
        (
            prepared.revisions,
            repository.revisions,
            revision.identifier,
            MAX_REVISION_BYTES,
        )
        for revision in revisions
    )
    for source, target, name, limit in objects:
        with staged_copy(source, target, name, limit, durable=True) as scratch:
            yield scratch


def publication(
    control: str, branch: str, base: str, prepared: str
) -> Generator[Scratch, None, None]:
    if not is_branch(branch):
        raise Refused
    opened = Opened()
    try:
        repository = opened.repository(control)
        store = opened_prepared(opened)
        yield from publish_objects(store, repository, base, prepared)
        parts = branch.split("/")
        heads = opened.directory(repository.root, "heads")
        parent = descend(heads, parts[:-1], opened)
        scratch = scratch_file(parent)
        write_all(scratch.descriptor, prepared.encode("ascii"))
        settle(scratch, parent, parts[-1], None, durable=True)
    finally:
        opened.close()


def publish(control: str, branch: str, base: str, prepared: str) -> None:
    with closing(publication(control, branch, base, prepared)) as stages:
        for _ in stages:
            continue


@contextmanager
def staged_copy(
    source: int, target: int, name: str, limit: int, durable: bool = False
) -> Generator[Scratch, None, None]:
    scratch = scratch_file(target)
    try:
        yield scratch
        descriptor = open_regular(source, name)
        try:
            digest, _ = pump(descriptor, scratch.descriptor, limit)
        finally:
            os.close(descriptor)
        if digest != name:
            raise Refused
        settle(scratch, target, name, name, durable)
    finally:
        discard(scratch)


def copy_object(source: int, target: int, name: str, limit: int) -> None:
    with staged_copy(source, target, name, limit):
        pass


class Arguments:
    def __init__(self, options: dict[str, str], revisions: list[str]) -> None:
        self.options = options
        self.revisions = revisions

    def text(self, name: str) -> str:
        return self.options[name]

    def identifier(self, name: str) -> str:
        return checked_id(self.options[name])

    def revision(self) -> str:
        return checked_id(self.revisions[0])

    def branch(self) -> str:
        branch = self.options["branch"]
        if not is_branch(branch):
            raise Refused
        return branch


def shown(arguments: Arguments) -> None:
    verify(arguments.text("repository"), [arguments.revision()])
    emit(arguments.revision())


Handler = Callable[[Arguments], None]
COMMANDS: Final[dict[str, tuple[tuple[str, ...], Handler]]] = {
    "repository": (("repository",), lambda a: check_repository(a.text("repository"))),
    "verify": (
        ("repository", "revision"),
        lambda a: verify(a.text("repository"), [checked_id(r) for r in a.revisions]),
    ),
    "materialise": (
        ("repository", "revision"),
        lambda a: materialise(a.text("repository"), a.revision()),
    ),
    "show": (("repository", "revision"), shown),
    "branch": (("branch",), lambda a: emit(a.branch())),
    "changed": (
        ("repository", "base", "revision"),
        lambda a: changed(a.text("repository"), a.identifier("base"), a.revision()),
    ),
    "commit": (("message",), lambda a: commit(a.text("message"))),
    "export": (
        ("checkout", "out"),
        lambda a: export(a.identifier("checkout"), a.text("out")),
    ),
    "ancestry": (
        ("repository", "base"),
        lambda a: check_ancestry(a.text("repository"), a.identifier("base")),
    ),
    "base": (
        ("repository", "base"),
        lambda a: copy_base(a.text("repository"), a.identifier("base")),
    ),
    "import": (
        ("input", "base"),
        lambda a: import_chain(a.text("input"), a.identifier("base")),
    ),
    "build": (
        ("input", "base", "message"),
        lambda a: build(a.text("input"), a.identifier("base"), a.text("message")),
    ),
    "prepared": ((), lambda a: prepared_head()),
    "decode": (
        ("base", "prepared"),
        lambda a: decode(a.identifier("base"), a.identifier("prepared")),
    ),
    "paths": (
        ("base", "prepared"),
        lambda a: paths(a.identifier("base"), a.identifier("prepared")),
    ),
    "stat": (
        ("base", "prepared"),
        lambda a: diff_stat(a.identifier("base"), a.identifier("prepared")),
    ),
    "publish": (
        ("repository", "branch", "base", "prepared"),
        lambda a: publish(
            a.text("repository"),
            a.branch(),
            a.identifier("base"),
            a.identifier("prepared"),
        ),
    ),
}


def parsed_arguments(arguments: Sequence[str]) -> tuple[Handler, Arguments]:
    if not arguments or arguments[0] not in COMMANDS or len(arguments) % 2 == 0:
        raise Refused
    allowed, handler = COMMANDS[arguments[0]]
    options: dict[str, str] = {}
    revisions: list[str] = []
    for option, value in zip(arguments[1::2], arguments[2::2], strict=True):
        name = option.removeprefix("--")
        if option == name or name not in allowed or name in options:
            raise Refused
        if name == "revision":
            revisions.append(value)
        else:
            options[name] = value
    if set(allowed) - options.keys() - ({"revision"} if revisions else set()):
        raise Refused
    return handler, Arguments(options, revisions)


def terminate(number: int, frame: FrameType | None) -> None:
    sys.exit(128 + number)


def main(arguments: list[str]) -> int:
    signal.signal(signal.SIGTERM, terminate)
    try:
        handler, parsed = parsed_arguments(arguments)
        handler(parsed)
    except (Refused, ValueError, OSError):
        return REFUSED
    except Failed:
        return FAILED
    finally:
        discard_all()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
