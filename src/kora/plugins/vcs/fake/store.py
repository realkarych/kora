import hashlib
import json
import os
import re
import signal
import stat
import sys
import unicodedata
from bisect import bisect_left
from collections.abc import Callable, Sequence
from types import FrameType
from typing import Final, NamedTuple, TypeGuard, cast

KIB: Final[int] = 1024
MIB: Final[int] = 1024 * KIB
MAX_MESSAGE_BYTES: Final[int] = 64 * KIB
MAX_REVISION_BYTES: Final[int] = 80 * KIB
MAX_TREE_ENTRIES: Final[int] = 50_000
MAX_TREE_BYTES: Final[int] = 64 * MIB
MAX_BLOB_BYTES: Final[int] = 256 * MIB
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
        isinstance(message, str) and len(message.encode("utf-8")) <= MAX_MESSAGE_BYTES
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


def read_limited(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, READ_FLAGS, dir_fd=directory)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
            raise Refused
        chunks: list[bytes] = []
        size = 0
        while block := os.read(descriptor, min(MIB, limit + 1 - size)):
            size += len(block)
            if size > limit:
                raise Refused
            chunks.append(block)
        return b"".join(chunks)
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
        and not any(ord(item) < 0x20 or ord(item) == 0x7F for item in part)
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
