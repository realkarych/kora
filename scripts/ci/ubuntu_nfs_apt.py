from __future__ import annotations

import asyncio
import ctypes
import fcntl
import hashlib
import os
import pwd
import re
import stat
import sys
import tempfile
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from itertools import islice
from pathlib import Path
from typing import Protocol, cast

SOURCE = Path("/etc/apt/sources.list.d/ubuntu.sources")
MIRRORS = Path("/etc/apt/apt-mirrors.txt")
SIGNER = Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg")
RUNTIME = Path("/run")
SIGNER_SIZE = 3607
SIGNER_DIGEST = "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
SOURCE_LIMIT = 16 * 1024
APT_OPTIONS = (
    "-q",
    "-o",
    "Acquire::Retries=0",
    "-o",
    "Acquire::http::Timeout=20",
    "-o",
    "Acquire::https::Timeout=20",
)
FIELD = re.compile(rb"([A-Za-z-]+:[ \t]*)([^\r\n]*?)([ \t]*\n?)")
SUITE = rb"noble(?:-updates|-backports|-security)?"
COMPONENT = rb"(?:main|universe|restricted|multiverse)"
REQUIRED_VALUES = {
    b"types": re.compile(rb"deb"),
    b"uris": re.compile(rb"mirror\+file:/etc/apt/apt-mirrors\.txt"),
    b"signed-by": re.compile(rb"/usr/share/keyrings/ubuntu-archive-keyring\.gpg"),
    b"suites": re.compile(SUITE + rb"(?:[ \t]+" + SUITE + rb")*"),
    b"components": re.compile(COMPONENT + rb"(?:[ \t]+" + COMPONENT + rb")*"),
}
OVERRIDE = re.compile(
    rb"(?:binary::apt-get::)?(?:apt::sources::with(?:::.*)?|rootdir)",
    re.IGNORECASE,
)
ATTEMPT_NAME = re.compile(r"attempt-[a-z0-9_]{8}")
GROUP = "group"
GROUP_PENDING = "group.pending"
STAGE_FILES = frozenset((SOURCE.name, SIGNER.name, GROUP_PENDING, GROUP))
PERMISSIONS = 0o022
SET_CHILD_SUBREAPER = 36

if sys.platform == "linux":
    PATH_ONLY = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
else:
    PATH_ONLY = 0


class Refused(RuntimeError):
    pass


def directory(status: os.stat_result, protected: bool) -> None:
    if not stat.S_ISDIR(status.st_mode):
        raise Refused("expected an input directory")
    if protected and (status.st_uid != 0 or status.st_mode & PERMISSIONS):
        raise Refused("a directory is outside root custody")


def regular(status: os.stat_result, protected: bool) -> None:
    if not stat.S_ISREG(status.st_mode) or status.st_uid != 0:
        raise Refused("expected a root-owned regular file")
    if protected and status.st_mode & PERMISSIONS:
        raise Refused("a file is outside root custody")


def identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


@contextmanager
def metadata(
    path: Path, protected: bool
) -> Generator[tuple[int, tuple[tuple[int, int], ...]]]:
    with ExitStack() as opened:
        descriptor = os.open("/", PATH_ONLY | os.O_DIRECTORY)
        opened.callback(os.close, descriptor)
        chain = [identity(os.fstat(descriptor))]
        directory(os.fstat(descriptor), protected)
        for index, part in enumerate(path.parts[1:], 2):
            descriptor = os.open(part, PATH_ONLY, dir_fd=descriptor)
            opened.callback(os.close, descriptor)
            status = os.fstat(descriptor)
            if index < len(path.parts):
                directory(status, protected)
            chain.append(identity(status))
        yield descriptor, tuple(chain)


def reopen(descriptor: int, flags: int) -> int:
    opened = os.open(
        f"/proc/self/fd/{descriptor}", flags | os.O_CLOEXEC | os.O_NONBLOCK
    )
    if identity(os.fstat(opened)) != identity(os.fstat(descriptor)):
        os.close(opened)
        raise Refused("an input descriptor changed")
    return opened


def read_at_most(descriptor: int, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = os.read(descriptor, count - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def content_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (*identity(status), status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def read_input(
    path: Path, limit: int, *, protected: bool, exact: bool = False
) -> bytes:
    with metadata(path, protected) as (handle, original_chain):
        original = os.fstat(handle)
        regular(original, protected)
        if not 0 < original.st_size <= limit or (exact and original.st_size != limit):
            raise Refused("an input has an unexpected size")
        descriptor = reopen(handle, os.O_RDONLY)
        try:
            contents = read_at_most(descriptor, limit + 1)
            if content_identity(os.fstat(descriptor)) != content_identity(original):
                raise Refused("an input changed during capture")
        finally:
            os.close(descriptor)
        with metadata(path, protected) as (current, current_chain):
            if original_chain != current_chain or content_identity(
                os.fstat(current)
            ) != content_identity(original):
                raise Refused("an input pathname changed during capture")
    if not 0 < len(contents) <= limit or (exact and len(contents) != limit):
        raise Refused("an input has an unexpected size")
    return contents


def admit_signer(contents: bytes) -> None:
    if (
        len(contents) != SIGNER_SIZE
        or hashlib.sha256(contents).hexdigest() != SIGNER_DIGEST
    ):
        raise Refused("Ubuntu archive signer does not match the approved pin")


def rewrite_source(contents: bytes, signer: bytes, previous: bytes) -> bytes:
    if b"\r" in contents or b"\0" in contents:
        raise Refused("an Ubuntu source uses unsupported line boundaries")
    parts = contents.split(b"\n")
    lines = [line + b"\n" for line in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    found: dict[bytes, bytes] = {}
    entry_count = 0
    for index, line in enumerate((*lines, b"\n")):
        if line.startswith(b"#"):
            continue
        if line == b"\n":
            if found:
                if found.keys() != REQUIRED_VALUES.keys():
                    raise Refused("an Ubuntu source stanza is incomplete")
                found.clear()
                entry_count += 1
            continue
        match = FIELD.fullmatch(line)
        if match is None:
            raise Refused("an Ubuntu source field has unsupported syntax")
        prefix, value, suffix = match.groups()
        name = prefix.partition(b":")[0].lower()
        expected = REQUIRED_VALUES.get(name)
        if expected is None or name in found:
            raise Refused("an Ubuntu source field is unexpected or duplicated")
        if name == b"signed-by":
            if value != previous:
                raise Refused("the Ubuntu source names a different signer")
            lines[index] = prefix + signer + suffix
        elif expected.fullmatch(value) is None:
            raise Refused("an Ubuntu source field has an unexpected value")
        found[name] = value
    if entry_count == 0:
        raise Refused("an Ubuntu source stanza is unavailable")
    return b"".join(lines)


def directories(descriptor: int, limit: int) -> tuple[str, ...]:
    with os.scandir(descriptor) as entries:
        names = tuple(entry.name for entry in islice(entries, limit + 1))
    if len(names) > limit:
        raise Refused("staging contains extra entries")
    return names


def check_name(parent: int, name: str, held: int) -> None:
    if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(
        os.fstat(held)
    ):
        raise Refused("a staging object was replaced")


@contextmanager
def owned_directory(parent: int, name: str) -> Generator[int]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
    )
    try:
        directory(os.fstat(descriptor), True)
        check_name(parent, name, descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def stage_entries(parent: int) -> Generator[tuple[tuple[str, int], ...]]:
    with ExitStack() as opened:
        entries: list[tuple[str, int]] = []
        for name in directories(parent, len(STAGE_FILES)):
            if name not in STAGE_FILES:
                raise Refused("staging contains an unexpected entry")
            descriptor = os.open(name, PATH_ONLY, dir_fd=parent)
            opened.callback(os.close, descriptor)
            regular(os.fstat(descriptor), True)
            check_name(parent, name, descriptor)
            entries.append((name, descriptor))
        yield tuple(entries)


def require_group_absent(held: int) -> None:
    descriptor = reopen(held, os.O_RDONLY)
    try:
        text = read_at_most(descriptor, 11)
        if re.fullmatch(rb"[1-9][0-9]{0,9}", text) is None:
            raise Refused("the staged acquisition group record is invalid")
        number = int(text)
        if not 1 < number <= 2**31 - 1:
            raise Refused("the staged acquisition group record is invalid")
        try:
            os.killpg(number, 0)
        except ProcessLookupError:
            return
        raise Refused("the staged acquisition group can still use the snapshot")
    finally:
        os.close(descriptor)


def remove_attempt(
    anchor: int, child: int, name: str, entries: Sequence[tuple[str, int]]
) -> None:
    check_name(anchor, name, child)
    for entry, descriptor in entries:
        check_name(child, entry, descriptor)
    for entry, _ in sorted(entries, key=lambda item: item[0] == GROUP):
        os.unlink(entry, dir_fd=child)
    os.rmdir(name, dir_fd=anchor)


def clear_anchor(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    with owned_directory(parent, name) as anchor, ExitStack() as opened:
        owned: list[tuple[str, int, tuple[tuple[str, int], ...]]] = []
        for child in directories(anchor, 3):
            if ATTEMPT_NAME.fullmatch(child) is None:
                raise Refused("the job staging anchor contains an unexpected object")
            descriptor = opened.enter_context(owned_directory(anchor, child))
            entries = opened.enter_context(stage_entries(descriptor))
            for entry, held in entries:
                if entry == GROUP:
                    require_group_absent(held)
            owned.append((child, descriptor, entries))
        for child, descriptor, entries in owned:
            remove_attempt(anchor, descriptor, child, entries)
        check_name(parent, name, anchor)
        os.rmdir(name, dir_fd=parent)


def lock_file(parent: int, name: str) -> int:
    try:
        return os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
    except FileExistsError:
        held = os.open(name, PATH_ONLY, dir_fd=parent)
        try:
            status = os.fstat(held)
            regular(status, True)
            if stat.S_IMODE(status.st_mode) != 0o600:
                raise Refused("the job staging control mode differs")
            return reopen(held, os.O_RDWR)
        finally:
            os.close(held)


def job_name() -> str:
    run = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    nonce = os.environ.get("KORA_RUNNER_UBUNTU_APT_NONCE", "")
    if (
        re.fullmatch(r"[1-9][0-9]{0,19}", run) is None
        or re.fullmatch(r"[1-9][0-9]{0,19}", attempt) is None
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
    ):
        raise Refused("the job staging identity is invalid")
    return f"kora-local-runtime-ubuntu-apt-{run}-{attempt}-{nonce}"


@contextmanager
def locked_job(name: str, *, closing: bool) -> Generator[int]:
    with metadata(RUNTIME, True) as (runtime, _):
        directory(os.fstat(runtime), True)
        parent = reopen(runtime, os.O_RDONLY | os.O_DIRECTORY)
        try:
            lock_name = f"{name}.lock"
            lock = lock_file(parent, lock_name)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                check_name(parent, lock_name, lock)
                status = os.fstat(lock)
                regular(status, True)
                state = read_at_most(lock, 2)
                if stat.S_IMODE(status.st_mode) != 0o600 or state not in {b"", b"x"}:
                    raise Refused("the job staging control object is invalid")
                if closing:
                    if os.pwrite(lock, b"x", 0) != 1 or os.pread(lock, 2, 0) != b"x":
                        raise Refused("could not close job staging admission")
                elif state:
                    raise Refused("job staging admission is closed")
                yield parent
            finally:
                os.close(lock)
        finally:
            os.close(parent)


def write_staged(parent: int, name: str, contents: bytes, mode: int) -> None:
    descriptor = os.open(
        name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    with os.fdopen(descriptor, "w+b") as staged:
        staged.write(contents)
        staged.seek(0)
        if staged.read(len(contents) + 1) != contents:
            raise Refused("staging could not verify the written snapshot")
        check_name(parent, name, descriptor)
        os.fchmod(descriptor, mode)


async def run(*args: str) -> int:
    process = await asyncio.create_subprocess_exec(*args)
    return await process.wait()


async def check_apt_scope() -> None:
    if os.environ.get("APT_CONFIG"):
        raise Refused("an APT configuration override is set")
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/apt-config",
        "dump",
        "--format",
        "%f%n",
        "--empty",
        stdout=asyncio.subprocess.PIPE,
    )
    keys, _ = await process.communicate()
    if process.returncode or any(OVERRIDE.fullmatch(key) for key in keys.splitlines()):
        raise Refused("APT configures a path root or additional package inputs")


async def check_readers(paths: Sequence[Path]) -> None:
    apt = pwd.getpwnam("_apt")
    for path in paths:
        if await run(
            "/usr/bin/setpriv",
            f"--reuid={apt.pw_uid}",
            f"--regid={apt.pw_gid}",
            "--clear-groups",
            "/usr/bin/test",
            "-r",
            str(path),
        ):
            raise Refused("an APT input is unreadable by _apt")


async def check_checkout_paths(paths: Sequence[Path]) -> None:
    uid = os.environ.get("SUDO_UID", "")
    if re.fullmatch(r"[1-9][0-9]{0,9}", uid) is None:
        raise Refused("the checkout identity is invalid")
    checkout = pwd.getpwuid(int(uid))
    if await run(
        "/usr/bin/setpriv",
        f"--reuid={checkout.pw_uid}",
        f"--regid={checkout.pw_gid}",
        "--init-groups",
        "/usr/bin/python3",
        "-I",
        "-B",
        "-c",
        "import os, sys; sys.exit(any(os.access(p, os.W_OK) for p in sys.argv[1:]))",
        *(str(path) for path in paths),
    ):
        raise Refused("a protected input or staging path is writable by checkout")


async def package_attempt(packages: Sequence[str], source: Path | None = None) -> int:
    scope = ()
    update_options = ()
    if source is not None:
        scope = (
            "-o",
            f"Dir::Etc::sourcelist={source}",
            "-o",
            "Dir::Etc::sourceparts=/dev/null",
        )
        update_options = ("--no-list-cleanup",)
    if next(Path("/var/lib/dpkg/updates").glob("[!.]*"), None) is not None:
        sys.stderr.write("Recovering an interrupted dpkg run\n")
        recovery = await run("/usr/bin/dpkg", "--configure", "-a")
        if recovery:
            sys.stderr.write("dpkg could not recover from an interrupted run\n")
            return recovery if source is not None and recovery < 0 else 1
    if source is not None:
        await check_apt_scope()
    update = await run(
        "/usr/bin/apt-get", *APT_OPTIONS, *scope, *update_options, "update"
    )
    if update:
        return update
    if source is not None:
        await check_apt_scope()
    return await run(
        "/usr/bin/apt-get",
        *APT_OPTIONS,
        *scope,
        "install",
        "--yes",
        "--no-install-recommends",
        *packages,
    )


async def publish_stage(descriptor: int, path: Path) -> None:
    signer = read_input(SIGNER, SIGNER_SIZE, protected=False, exact=True)
    admit_signer(signer)
    source = read_input(SOURCE, SOURCE_LIMIT, protected=True)
    with metadata(MIRRORS, True) as (mirror, _):
        status = os.fstat(mirror)
        regular(status, True)
        if status.st_size <= 0:
            raise Refused("the Ubuntu mirror list is empty")
    original = os.fsencode(SIGNER)
    staged = os.fsencode(path / SIGNER.name)
    scoped_source = rewrite_source(source, staged, original)
    if rewrite_source(scoped_source, original, staged) != source:
        raise Refused("the staged Ubuntu source changes fields beyond Signed-By")
    write_staged(descriptor, SIGNER.name, signer, 0o644)
    write_staged(descriptor, SOURCE.name, scoped_source, 0o644)
    os.fchmod(descriptor, 0o755)
    await check_readers((path / SIGNER.name, path / SOURCE.name, MIRRORS))
    await check_checkout_paths(
        (
            path / SIGNER.name,
            path / SOURCE.name,
            *path.parents,
            path,
            SOURCE,
            *SOURCE.parents,
            MIRRORS,
        )
    )
    await check_apt_scope()


class ProcessControl(Protocol):
    def prctl(
        self,
        operation: ctypes.c_int,
        setting: ctypes.c_ulong,
        unused1: ctypes.c_ulong,
        unused2: ctypes.c_ulong,
        unused3: ctypes.c_ulong,
        /,
    ) -> int: ...


def adopt_scoped_children() -> None:
    libc = cast(ProcessControl, ctypes.CDLL(None, use_errno=True))
    unused = ctypes.c_ulong(0)
    if libc.prctl(
        ctypes.c_int(SET_CHILD_SUBREAPER), ctypes.c_ulong(1), unused, unused, unused
    ):
        raise Refused("cannot establish scoped package reader ownership")


def require_readers_joined(group: int) -> None:
    while True:
        try:
            reaped, _ = os.waitpid(-group, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped == 0:
            raise Refused("a scoped package reader has not settled")


def remove_settled_attempt(anchor: int, child: int, name: str, group: int) -> None:
    require_readers_joined(group)
    with stage_entries(child) as entries:
        remove_attempt(anchor, child, name, entries)


async def scoped_attempt() -> int:
    group = os.getpgrp()
    if group != os.getpid() or group != os.getsid(0):
        raise Refused("the scoped package attempt does not own its process group")
    name = job_name()
    with locked_job(name, closing=False) as parent:
        clear_anchor(parent, name)
        os.mkdir(name, 0o755, dir_fd=parent)
        with owned_directory(parent, name) as anchor:
            os.fchmod(anchor, 0o755)
            path = Path(tempfile.mkdtemp(prefix="attempt-", dir=RUNTIME / name))
            with owned_directory(anchor, path.name) as child:
                try:
                    await publish_stage(child, path)
                    check_name(anchor, path.name, child)
                    adopt_scoped_children()
                    write_staged(
                        child, GROUP_PENDING, str(group).encode("ascii"), 0o600
                    )
                    os.rename(GROUP_PENDING, GROUP, src_dir_fd=child, dst_dir_fd=child)
                except (OSError, Refused, KeyError):
                    try:
                        os.stat(GROUP, dir_fd=child, follow_symlinks=False)
                    except FileNotFoundError:
                        with stage_entries(child) as entries:
                            remove_attempt(anchor, child, path.name, entries)
                    raise
                sys.stdout.write(
                    "NFS acquisition uses the pinned Ubuntu archive signer "
                    "and runner Noble sources\n"
                )
                sys.stdout.flush()
                try:
                    result = await package_attempt(
                        ("nfs-kernel-server",), path / SOURCE.name
                    )
                except Refused:
                    remove_settled_attempt(anchor, child, path.name, group)
                    raise
                if result in (0, 1, 100):
                    remove_settled_attempt(anchor, child, path.name, group)
                return result


async def main(arguments: Sequence[str]) -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise Refused("package acquisition requires root on Linux")
    if tuple(arguments) == ("--scoped-nfs",):
        return await scoped_attempt()
    if tuple(arguments) == ("--cleanup-nfs",):
        name = job_name()
        with locked_job(name, closing=True) as parent:
            clear_anchor(parent, name)
        sys.stdout.write("Ubuntu NFS staging is absent\n")
        return 0
    if arguments and arguments[0] == "--default" and len(arguments) > 1:
        return await package_attempt(arguments[1:])
    raise Refused("invalid package acquisition invocation")


if __name__ == "__main__":
    try:
        outcome = asyncio.run(main(sys.argv[1:]))
        sys.exit(outcome if outcome >= 0 else 128 - outcome)
    except Refused as error:
        sys.stderr.write(f"Runner Ubuntu APT inputs refused: {error}\n")
        sys.exit(1)
    except (OSError, KeyError):
        sys.stderr.write(
            "Ubuntu package acquisition could not access an input or owned staging\n"
        )
        sys.exit(1)
