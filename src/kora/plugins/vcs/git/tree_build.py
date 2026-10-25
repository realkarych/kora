import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import struct
import subprocess
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import IO, Final, cast

OBJECT_ID: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SIGNATURES: Final[tuple[bytes, ...]] = (b"# v2 git bundle\n", b"# v3 git bundle\n")
PACK_HEADER: Final[struct.Struct] = struct.Struct(">4sII")
MAX_HEADER_BYTES: Final[int] = 1_048_576
MIB: Final[int] = 1_048_576
MODES: Final[frozenset[str]] = frozenset({"100644", "100755", "120000", "160000"})
GITLINK: Final[str] = "160000"
RESERVED: Final[frozenset[str]] = frozenset({".git", ".kora", "git~1", "kora~1"})
IGNORABLE: Final[frozenset[str]] = frozenset("‌‍‎‏‪‫‬‭‮⁪⁫⁬⁭⁮⁯﻿")
HEAD_REF: Final[str] = "refs/kora/head"
BASE_REF: Final[str] = "refs/kora/base"
PREPARED_REF: Final[str] = "refs/kora/prepared"
REFUSED: Final[int] = 3
FAILED: Final[int] = 4


class Refused(Exception):
    pass


class Failed(Exception):
    pass


@dataclass(frozen=True)
class Options:
    git: str
    command: str
    repo: str
    input: str
    base: str
    max_bundle: int
    max_objects: int
    message: str
    name: str
    email: str
    date: str
    max_records: int
    max_blob: int


@dataclass(frozen=True)
class Record:
    path: str
    mode: str
    size: int
    sha256: str | None
    chunk: int | None
    offset: int | None
    native: str | None


@dataclass
class Children:
    processes: list[subprocess.Popen[bytes]]
    scratch: Path | None = None

    def stop(self) -> None:
        for process in self.processes:
            stream = process.stdin
            if stream is not None and not stream.closed:
                stream.close()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.wait()
        self.processes.clear()
        if self.scratch is not None and self.scratch.exists():
            self.scratch.unlink()


CHILDREN: Final[Children] = Children([])


def terminate(number: int, frame: FrameType | None) -> None:
    CHILDREN.stop()
    sys.exit(128 + number)


def tracked(argv: list[str], environment: dict[str, str] | None) -> tuple[int, bytes]:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        env=environment,
    )
    CHILDREN.processes.append(process)
    output, _ = process.communicate()
    CHILDREN.processes.remove(process)
    return process.returncode, output


class Git:
    def __init__(self, binary: str, repository: str) -> None:
        self.binary = binary
        self.repository = repository

    def argv(self, arguments: Sequence[str]) -> list[str]:
        return [self.binary, f"--git-dir={self.repository}", *arguments]

    def output(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> bytes:
        code, output = tracked(self.argv(arguments), environment)
        if code != 0:
            raise Failed
        return output

    def succeeds(self, *arguments: str) -> bool:
        code, _ = tracked(self.argv(arguments), None)
        return code == 0

    def object_id(self, *arguments: str) -> str:
        return checked_id(self.output(*arguments).strip().decode("ascii", "replace"))

    def start(
        self, arguments: Sequence[str], environment: dict[str, str] | None = None
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            self.argv(arguments),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            env=environment,
        )
        CHILDREN.processes.append(process)
        return process


def checked_id(value: object) -> str:
    if not isinstance(value, str) or OBJECT_ID.fullmatch(value) is None:
        raise Refused
    return value


def json_object(data: bytes) -> dict[str, object]:
    document: object = json.loads(data)
    if not isinstance(document, dict):
        raise Refused
    return cast("dict[str, object]", document)


def read_head(sealed: Path) -> tuple[str, bool]:
    fields = json_object((sealed / "head.json").read_bytes())
    bundle = fields.get("bundle")
    if not isinstance(bundle, bool):
        raise Refused
    return checked_id(fields.get("head")), bundle


@dataclass(frozen=True)
class BundleHeader:
    prerequisites: tuple[str, ...]
    references: tuple[tuple[str, str], ...]
    objects: int


def bundle_header(path: Path) -> BundleHeader:
    with path.open("rb") as stream:
        signature = stream.readline(MAX_HEADER_BYTES)
        if signature not in SIGNATURES:
            raise Refused
        prerequisites: list[str] = []
        references: list[tuple[str, str]] = []
        consumed = len(signature)
        while True:
            line = stream.readline(MAX_HEADER_BYTES)
            consumed += len(line)
            if not line.endswith(b"\n") or consumed > MAX_HEADER_BYTES:
                raise Refused
            text = line[:-1].decode("utf-8", "replace")
            if not text:
                break
            if text.startswith("@"):
                continue
            if text.startswith("-"):
                prerequisites.append(checked_id(text[1:].split(" ", 1)[0]))
                continue
            oid, _, name = text.partition(" ")
            references.append((checked_id(oid), name))
        header = stream.read(PACK_HEADER.size)
    if len(header) != PACK_HEADER.size:
        raise Refused
    magic, _, count = PACK_HEADER.unpack(header)
    if magic != b"PACK":
        raise Refused
    return BundleHeader(tuple(prerequisites), tuple(references), count)


def import_head(options: Options) -> None:
    repository = Git(options.git, options.repo)
    sealed = Path(options.input)
    base = checked_id(options.base)
    head, bundled = read_head(sealed)
    if not bundled:
        if head != base:
            raise Refused
        repository.output("update-ref", HEAD_REF, base)
        return
    bundle = sealed / "head.bundle"
    if bundle.stat().st_size > options.max_bundle:
        raise Refused
    header = bundle_header(bundle)
    if header.prerequisites != (base,) or len(header.references) != 1:
        raise Refused
    if header.references[0][0] != head or header.objects > options.max_objects:
        raise Refused
    if not repository.succeeds("bundle", "verify", "--quiet", str(bundle)):
        raise Refused
    name = header.references[0][1]
    if not repository.succeeds(
        "fetch",
        "--quiet",
        "--no-tags",
        "--end-of-options",
        str(bundle),
        f"+{name}:{HEAD_REF}",
    ):
        raise Refused
    if repository.object_id("rev-parse", "--verify", f"{HEAD_REF}^0") != head:
        raise Refused
    if not repository.succeeds("merge-base", "--is-ancestor", BASE_REF, HEAD_REF):
        raise Refused


def reserved(component: str) -> bool:
    visible = "".join(item for item in component if item not in IGNORABLE)
    trimmed = visible.split(":", 1)[0].rstrip(". ")
    return unicodedata.normalize("NFC", trimmed).casefold() in RESERVED


def path_allowed(path: str) -> bool:
    if not path or path.startswith("/") or "\x00" in path or "\n" in path:
        return False
    return all(
        part not in ("", ".", "..") and not reserved(part) for part in path.split("/")
    )


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Refused
    return value


def optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Refused
    return value


def parsed_record(line: bytes) -> Record:
    fields = json_object(line)
    path, mode, size = fields.get("path"), fields.get("mode"), fields.get("size")
    if not isinstance(path, str) or not path_allowed(path) or mode not in MODES:
        raise Refused
    if not isinstance(mode, str):
        raise Refused
    record = Record(
        path=path,
        mode=mode,
        size=optional_int(size) or 0,
        sha256=optional_text(fields.get("sha256")),
        chunk=optional_int(fields.get("chunk")),
        offset=optional_int(fields.get("offset")),
        native=optional_text(fields.get("object")),
    )
    if (record.mode == GITLINK) != (record.native is not None):
        raise Refused
    if record.mode != GITLINK and (record.chunk is None or record.offset is None):
        raise Refused
    return record


def tree_records(sealed: Path, limit: int) -> list[Record]:
    records: list[Record] = []
    with (sealed / "tree.jsonl").open("rb") as stream:
        for line in stream:
            records.append(parsed_record(line))
            if len(records) > limit:
                raise Refused
    if len({record.path for record in records}) != len(records):
        raise Refused
    return records


def copy_record(sealed: Path, record: Record, scratch: Path, limit: int) -> None:
    if record.size > limit or record.chunk is None or record.offset is None:
        raise Refused
    digest = hashlib.sha256()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    target = os.open(scratch, flags, 0o600)
    try:
        with (sealed / f"tree.{record.chunk}.bin").open("rb") as source:
            source.seek(record.offset)
            remaining = record.size
            while remaining:
                block = source.read(min(MIB, remaining))
                if not block:
                    raise Refused
                remaining -= len(block)
                digest.update(block)
                view = memoryview(block)
                while view:
                    view = view[os.write(target, view) :]
    finally:
        os.close(target)
    if "sha256:" + digest.hexdigest() != record.sha256:
        raise Refused


def send(stream: IO[bytes] | None, data: bytes) -> None:
    if stream is None:
        raise Failed
    stream.write(data)
    stream.flush()


def build_index(options: Options, environment: dict[str, str]) -> int:
    repository = Git(options.git, options.repo)
    sealed = Path(options.input)
    records = tree_records(sealed, options.max_records)
    hasher = repository.start(("hash-object", "-w", "--no-filters", "--stdin-paths"))
    updater = repository.start(("update-index", "-z", "--index-info"), environment)
    work = Path.cwd()
    for record in records:
        if record.native is not None:
            object_id = checked_id(record.native)
        else:
            scratch = work / f"scratch-{secrets.token_hex(8)}"
            CHILDREN.scratch = scratch
            copy_record(sealed, record, scratch, options.max_blob)
            send(hasher.stdin, str(scratch).encode("utf-8") + b"\n")
            if hasher.stdout is None:
                raise Failed
            object_id = checked_id(hasher.stdout.readline().strip().decode("ascii"))
            scratch.unlink()
            CHILDREN.scratch = None
        entry = f"{record.mode} {object_id}\t{record.path}".encode() + b"\x00"
        send(updater.stdin, entry)
    for process in (hasher, updater):
        if process.stdin is not None:
            process.stdin.close()
        if process.wait() != 0:
            raise Failed
    CHILDREN.processes.clear()
    listed = repository.output("ls-files", "-z", "--stage", environment=environment)
    return len([entry for entry in listed.split(b"\x00") if entry]) - len(records)


def build(options: Options) -> None:
    repository = Git(options.git, options.repo)
    base = checked_id(options.base)
    index = Path.cwd() / f"index-{secrets.token_hex(8)}"
    environment = {**os.environ, "GIT_INDEX_FILE": str(index)}
    try:
        if build_index(options, environment) != 0:
            raise Refused
        tree = checked_id(
            repository.output("write-tree", environment=environment)
            .strip()
            .decode("ascii")
        )
    finally:
        if index.exists():
            index.unlink()
    head = repository.object_id("rev-parse", "--verify", f"{HEAD_REF}^0")
    head_tree = repository.object_id("rev-parse", "--verify", f"{HEAD_REF}^{{tree}}")
    prepared = head
    if tree != head_tree:
        identity = {
            **os.environ,
            "GIT_AUTHOR_NAME": options.name,
            "GIT_AUTHOR_EMAIL": options.email,
            "GIT_AUTHOR_DATE": options.date,
            "GIT_COMMITTER_NAME": options.name,
            "GIT_COMMITTER_EMAIL": options.email,
            "GIT_COMMITTER_DATE": options.date,
        }
        prepared = checked_id(
            repository.output(
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                options.message,
                environment=identity,
            )
            .strip()
            .decode("ascii")
        )
    if prepared == base:
        raise Refused
    repository.output("update-ref", PREPARED_REF, prepared)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="tree_build")
    root.add_argument("--git", required=True)
    commands = root.add_subparsers(dest="command", required=True)
    importing = commands.add_parser("import")
    building = commands.add_parser("build")
    for command in (importing, building):
        command.add_argument("--repo", required=True)
        command.add_argument("--input", required=True)
        command.add_argument("--base", required=True)
    importing.add_argument("--max-bundle", type=int, default=512 * MIB)
    importing.add_argument("--max-objects", type=int, default=200_000)
    building.add_argument("--message", required=True)
    building.add_argument("--name", required=True)
    building.add_argument("--email", required=True)
    building.add_argument("--date", required=True)
    building.add_argument("--max-records", type=int, default=50_000)
    building.add_argument("--max-blob", type=int, default=256 * MIB)
    return root


def options_of(arguments: list[str]) -> Options:
    namespace = parser().parse_args(arguments)
    values = vars(namespace)
    return Options(
        git=str(values["git"]),
        command=str(values["command"]),
        repo=str(values["repo"]),
        input=str(values["input"]),
        base=str(values["base"]),
        max_bundle=int(values.get("max_bundle", 512 * MIB)),
        max_objects=int(values.get("max_objects", 200_000)),
        message=str(values.get("message", "")),
        name=str(values.get("name", "")),
        email=str(values.get("email", "")),
        date=str(values.get("date", "")),
        max_records=int(values.get("max_records", 50_000)),
        max_blob=int(values.get("max_blob", 256 * MIB)),
    )


def main(arguments: list[str]) -> int:
    signal.signal(signal.SIGTERM, terminate)
    options = options_of(arguments)
    try:
        if options.command == "import":
            import_head(options)
        else:
            build(options)
    except (Refused, json.JSONDecodeError, UnicodeDecodeError, OSError):
        CHILDREN.stop()
        return REFUSED
    except Failed:
        CHILDREN.stop()
        return FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
