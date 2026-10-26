import argparse
import hashlib
import json
import re
import signal
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Final

OBJECT_ID: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
BLOB_MODES: Final[dict[str, str]] = {
    "100644": "file",
    "100755": "file",
    "120000": "symlink",
}
LFS_POINTER: Final[bytes] = b"version https://git-lfs.github.com/spec/v1"
LFS_ATTRIBUTE: Final[re.Pattern[bytes]] = re.compile(rb"(?:^|\s)filter=lfs(?:\s|$)")
MAX_POINTER_BYTES: Final[int] = 1024
PREPARED_DIGEST_FILE: Final[str] = "prepared_digest"
MIB: Final[int] = 1_048_576
REFUSED: Final[int] = 3
FAILED: Final[int] = 4


class Refused(Exception):
    pass


class Failed(Exception):
    pass


@dataclass(frozen=True)
class Options:
    git: str
    repo: str
    base: str
    prepared: str
    command: str
    out: str
    max_records: int
    max_blob: int


@dataclass(frozen=True)
class Version:
    revision: str
    path: str
    kind: str
    location: str


def checked_id(value: str) -> str:
    if OBJECT_ID.fullmatch(value) is None:
        raise Refused
    return value


CHILDREN: Final[list[subprocess.Popen[bytes]]] = []


def terminate(number: int, frame: FrameType | None) -> None:
    for process in list(CHILDREN):
        if process.poll() is None:
            process.terminate()
        process.wait()
    sys.exit(128 + number)


class Git:
    def __init__(self, binary: str, repository: str) -> None:
        self.argv = [binary, f"--git-dir={repository}"]

    def output(self, *arguments: str) -> bytes:
        process = subprocess.Popen(
            [*self.argv, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,
        )
        CHILDREN.append(process)
        output, _ = process.communicate()
        CHILDREN.remove(process)
        if process.returncode != 0:
            raise Failed
        return output

    def batch(self) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [*self.argv, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
        CHILDREN.append(process)
        return process


def new_commits(git: Git, prepared: str, base: str) -> list[tuple[str, list[str]]]:
    listed = git.output("rev-list", "--parents", prepared, f"^{base}", "--")
    commits: list[tuple[str, list[str]]] = []
    for line in listed.decode("ascii").splitlines():
        fields = [checked_id(field) for field in line.split()]
        commits.append((fields[0], fields[1:]))
    return commits


def stdin_output(git: Git, arguments: Sequence[str], data: bytes) -> bytes:
    process = subprocess.Popen(
        [*git.argv, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    CHILDREN.append(process)
    output, _ = process.communicate(data)
    CHILDREN.remove(process)
    if process.returncode != 0:
        raise Failed
    return output


def raw_entries(
    git: Git, commits: Sequence[tuple[str, list[str]]]
) -> Iterator[tuple[str, str, str, str]]:
    lines = "".join(
        " ".join([commit, *parents]) + "\n" for commit, parents in commits
    ).encode("ascii")
    output = stdin_output(
        git,
        ("diff-tree", "--stdin", "-m", "--root", "-r", "--raw", "-z", "--no-renames"),
        lines,
    )
    fields = output.split(b"\x00")
    known = {commit for commit, _ in commits}
    commit = ""
    index = 0
    while index < len(fields):
        field = fields[index].decode("ascii", "replace")
        if not field:
            index += 1
            continue
        if not field.startswith(":"):
            if field not in known:
                raise Refused
            commit = field
            index += 1
            continue
        if not commit or index + 1 >= len(fields):
            raise Refused
        _, new_mode, _, new_id, _ = field[1:].split(" ")
        yield commit, new_mode, checked_id(new_id), fields[index + 1].decode("utf-8")
        index += 2


def versions(
    git: Git, commits: Sequence[tuple[str, list[str]]]
) -> tuple[list[Version], set[str]]:
    found: dict[tuple[str, str], Version] = {}
    changed: set[str] = set()
    for commit, mode, object_id, path in raw_entries(git, commits):
        changed.add(path)
        kind = BLOB_MODES.get(mode)
        if kind is None or set(object_id) == {"0"}:
            continue
        found.setdefault((object_id, path), Version(commit, path, kind, object_id))
    return list(found.values()), changed


def reachable(git: Git, prepared: str, base: str) -> dict[str, str]:
    listed = git.output("rev-list", "--objects", prepared, f"^{base}", "--")
    named: dict[str, str] = {}
    for line in listed.decode("utf-8").splitlines():
        object_id, _, name = line.partition(" ")
        named[checked_id(object_id)] = name
    return named


@dataclass(frozen=True)
class Content:
    kind: str
    size: int
    digest: str
    pointer: bool
    attribute: bool


def read_object(
    process: subprocess.Popen[bytes], object_id: str, limit: int
) -> Content:
    if process.stdin is None or process.stdout is None:
        raise Failed
    process.stdin.write(object_id.encode("ascii") + b"\n")
    process.stdin.flush()
    header = process.stdout.readline().decode("ascii").split()
    if len(header) != 3 or header[0] != object_id:
        raise Refused
    size = int(header[2])
    if size > limit:
        raise Refused
    digest = hashlib.sha256()
    first = b""
    attribute = False
    tail = b""
    remaining = size
    while remaining:
        block = process.stdout.read(min(MIB, remaining))
        if not block:
            raise Refused
        remaining -= len(block)
        digest.update(block)
        if len(first) < MAX_POINTER_BYTES:
            first += block[: MAX_POINTER_BYTES - len(first)]
        window = tail + block
        attribute = attribute or any(
            LFS_ATTRIBUTE.search(line) is not None for line in window.splitlines()
        )
        tail = window[-64:]
    if process.stdout.read(1) != b"\n":
        raise Refused
    pointer = size <= MAX_POINTER_BYTES and first.startswith(LFS_POINTER)
    return Content(header[1], size, "sha256:" + digest.hexdigest(), pointer, attribute)


def refuses_lfs(path: str, content: Content) -> bool:
    attributes = Path(path).name == ".gitattributes" and content.attribute
    return attributes or content.pointer


def record_line(
    revision: str, path: str | None, kind: str, location: str, content: Content
) -> str:
    return json.dumps(
        {
            "revision": revision,
            "path": path,
            "kind": kind,
            "content_location": location,
            "content_digest": content.digest,
            "size_bytes": content.size,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def index_lines(git: Git, options: Options) -> list[str]:
    commits = new_commits(git, options.prepared, options.base)
    found, _ = versions(git, commits)
    named = reachable(git, options.prepared, options.base)
    covered = {version.location for version in found} | {
        commit for commit, _ in commits
    }
    extra = [
        Version(options.prepared, name, "file", object_id)
        for object_id, name in named.items()
        if object_id not in covered
    ]
    process = git.batch()
    try:
        lines: list[str] = []
        for commit, _ in commits:
            content = read_object(process, commit, options.max_blob)
            if content.kind != "commit":
                raise Refused
            lines.append(
                record_line(commit, None, "revision_metadata", commit, content)
            )
        contents: dict[str, Content] = {}
        for version in [*found, *extra]:
            if version.location not in named:
                continue
            content = contents.get(version.location)
            if content is None:
                content = read_object(process, version.location, options.max_blob)
                contents[version.location] = content
            if content.kind != "blob":
                continue
            if refuses_lfs(version.path, content):
                raise Refused
            lines.append(
                record_line(
                    version.revision,
                    version.path,
                    version.kind,
                    version.location,
                    content,
                )
            )
            if len(lines) > options.max_records:
                raise Refused
        return lines
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait()


def write_index(options: Options) -> None:
    git = Git(options.git, options.repo)
    lines = index_lines(git, options)
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    output = Path(options.out)
    if output.name != options.out:
        raise Refused
    output.write_bytes(data)
    prepared_digest = Path(PREPARED_DIGEST_FILE).read_text(encoding="ascii").strip()
    manifest = {
        "prepared_digest": prepared_digest,
        "revision": options.prepared,
        "content_index_location": options.out,
        "content_index_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "complete": True,
    }
    sys.stdout.write(json.dumps(manifest, sort_keys=True))


def write_paths(options: Options) -> None:
    git = Git(options.git, options.repo)
    _, changed = versions(git, new_commits(git, options.prepared, options.base))
    sys.stdout.write(json.dumps(sorted(changed), ensure_ascii=False))


def write_changed(options: Options) -> None:
    git = Git(options.git, options.repo)
    output = git.output(
        "diff-tree",
        "-r",
        "-z",
        "--no-renames",
        "--name-only",
        options.base,
        options.prepared,
        "--",
    )
    paths = sorted({entry.decode("utf-8") for entry in output.split(b"\x00") if entry})
    sys.stdout.write(json.dumps(paths, ensure_ascii=False))


def options_of(arguments: list[str]) -> Options:
    parser = argparse.ArgumentParser(prog="decoder")
    parser.add_argument("--git", required=True)
    parser.add_argument("command", choices=("index", "paths", "changed"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--out", default="index.jsonl")
    parser.add_argument("--max-records", type=int, default=100_000)
    parser.add_argument("--max-blob", type=int, default=256 * MIB)
    namespace = parser.parse_args(arguments)
    return Options(
        git=str(namespace.git),
        repo=str(namespace.repo),
        base=checked_id(str(namespace.base)),
        prepared=checked_id(str(namespace.prepared)),
        command=str(namespace.command),
        out=str(namespace.out),
        max_records=int(namespace.max_records),
        max_blob=int(namespace.max_blob),
    )


def main(arguments: list[str]) -> int:
    signal.signal(signal.SIGTERM, terminate)
    try:
        options = options_of(arguments)
        if options.command == "index":
            write_index(options)
        elif options.command == "paths":
            write_paths(options)
        else:
            write_changed(options)
    except (Refused, UnicodeDecodeError, ValueError, OSError):
        return REFUSED
    except Failed:
        return FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
