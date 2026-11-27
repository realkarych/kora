from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import stat
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path("/run/kora-browser-apt")
SOURCE = Path("/etc/apt/sources.list.d/ubuntu.sources")
SIGNER = Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg")
MIRRORS = Path("/etc/apt/apt-mirrors.txt")
MIRROR = b"https://archive.ubuntu.com/ubuntu\n"
SIGNER_SIZE = 3607
SIGNER_DIGEST = "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
SOURCE_LIMIT = 16 * 1024
PROFILE_LIMIT = 128 * 1024
SERVICE_NAME = re.compile(r"kora-browser-packages-[0-9a-f]{32}\.service")
INVOCATION = re.compile(r"[0-9a-f]{32}")
PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,63}")
INSTALL = ("install", "-y", "--no-install-recommends")
CONSOLE_PACKAGES = ("libnss3-tools", "xdg-utils")
PLAYWRIGHT_WITNESSES = frozenset(("refusal-witness", "unavailable-adapter-witness"))
PLAYWRIGHT_WORK = PLAYWRIGHT_WITNESSES | {"install"}
APT_GET = "/usr/bin/apt-get"
APT_CONFIG = "/usr/bin/apt-config"
NATIVE_APT_ERROR = 100
DPKG = "/usr/bin/dpkg"
PYTHON = "/usr/bin/python3"
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
MANAGER_ENV = {"PATH": SYSTEM_PATH, "LANG": "C.UTF-8", "SYSTEMD_COLORS": "0"}
APT_ENV = {**MANAGER_ENV, "DEBIAN_FRONTEND": "noninteractive"}
DEB822_FIELD = re.compile(rb"([A-Za-z-]+:[ \t]*)([^\r\n]*?)([ \t]*\n?)")
SUITES = frozenset((b"noble", b"noble-updates", b"noble-backports", b"noble-security"))
COMPONENTS = frozenset((b"main", b"universe", b"restricted", b"multiverse"))
SOURCE_FIELDS = frozenset((b"types", b"uris", b"suites", b"components", b"signed-by"))
FALSE_SETTINGS = frozenset(
    (
        "apt::get::allowunauthenticated",
        "acquire::allowinsecurerepositories",
        "acquire::allowdowngradetoinsecurerepositories",
        "acquire::allowweakrepositories",
    )
)
TRUE_SETTINGS = frozenset(("acquire::check-date", "acquire::check-valid-until"))
FALSE_VALUES = frozenset(("", "0", "false", "no", "off"))
TRUE_VALUES = frozenset(("1", "true", "yes", "on"))


class AptRefused(RuntimeError):
    pass


def admitted_owner(status: os.stat_result, *, hash_only: bool) -> bool:
    return status.st_uid == 0 and (hash_only or not status.st_mode & 0o022)


def check_directory(
    status: os.stat_result, subject: str, *, hash_only: bool = False
) -> None:
    if not stat.S_ISDIR(status.st_mode) or not admitted_owner(
        status, hash_only=hash_only
    ):
        raise AptRefused(f"{subject} directory is outside its required custody.")


def file_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


@contextmanager
def input_descriptor(
    path: Path, subject: str, *, hash_only: bool
) -> Generator[tuple[int, tuple[tuple[int, int], ...]]]:
    if sys.platform != "linux" or not path.is_absolute():
        raise AptRefused(f"{subject} path is unsupported.")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        chain: list[tuple[int, int]] = []
        for part in path.parts[1:]:
            status = os.fstat(descriptor)
            check_directory(status, subject, hash_only=hash_only)
            chain.append((status.st_dev, status.st_ino))
            child = os.open(
                part, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor, tuple(chain)
    finally:
        os.close(descriptor)


def read_input(
    path: Path, limit: int, subject: str, *, hash_only: bool = False
) -> bytes:
    with input_descriptor(path, subject, hash_only=hash_only) as (descriptor, chain):
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or not admitted_owner(status, hash_only=hash_only)
            or not 0 < status.st_size <= limit
        ):
            raise AptRefused(f"{subject} file is outside its required custody.")
        with Path(f"/proc/self/fd/{descriptor}").open("rb") as stream:
            contents = stream.read(limit + 1)
            if (
                file_identity(os.fstat(stream.fileno())) != file_identity(status)
                or len(contents) != status.st_size
            ):
                raise AptRefused(f"{subject} changed during capture.")
        with input_descriptor(path, subject, hash_only=hash_only) as (
            current,
            observed,
        ):
            if observed != chain or file_identity(os.fstat(current)) != file_identity(
                status
            ):
                raise AptRefused(f"{subject} pathname changed during capture.")
    return contents


def admit_signer(contents: bytes) -> None:
    if (
        len(contents) != SIGNER_SIZE
        or hashlib.sha256(contents).hexdigest() != SIGNER_DIGEST
    ):
        raise AptRefused("Ubuntu archive signer does not match the approved pin.")


def read_signer() -> bytes:
    contents = read_input(SIGNER, SIGNER_SIZE, "Noble signer input", hash_only=True)
    admit_signer(contents)
    return contents


def rewrite_sources(contents: bytes, signer: Path, mirrors: Path) -> bytes:
    if b"\r" in contents or b"\0" in contents:
        raise AptRefused("Ubuntu source uses unsupported line boundaries.")
    replacements = {
        b"uris": (
            b"mirror+file:" + os.fsencode(MIRRORS),
            b"mirror+file:" + os.fsencode(mirrors),
        ),
        b"signed-by": (os.fsencode(SIGNER), os.fsencode(signer)),
    }
    output: list[bytes] = []
    stanza: dict[bytes, bytes] = {}
    suites: set[bytes] = set()
    for line in (*contents.splitlines(keepends=True), b"\n"):
        if line == b"\n" and stanza:
            stanza_suites = stanza.get(b"suites", b"").split()
            components = stanza.get(b"components", b"").split()
            if (
                stanza.keys() != SOURCE_FIELDS
                or stanza[b"types"] != b"deb"
                or not (set(stanza_suites) <= SUITES)
                or not stanza_suites
                or len(stanza_suites) != len(set(stanza_suites))
                or suites.intersection(stanza_suites)
                or frozenset(components) != COMPONENTS
                or len(components) != len(COMPONENTS)
            ):
                raise AptRefused("Ubuntu source stanza differs from the Noble profile.")
            suites.update(stanza_suites)
            stanza.clear()
        if line != b"\n" and not line.startswith(b"#"):
            field = DEB822_FIELD.fullmatch(line)
            if field is None:
                raise AptRefused("Ubuntu source field uses unsupported syntax.")
            prefix, value, suffix = field.groups()
            name = prefix.partition(b":")[0].lower()
            if name in stanza:
                raise AptRefused("Ubuntu source field is duplicated.")
            stanza[name] = value
            if name in replacements:
                previous, replacement = replacements[name]
                if value != previous:
                    raise AptRefused("Ubuntu source names another mirror or signer.")
                line = prefix + replacement + suffix
        output.append(line)
    if frozenset(suites) != SUITES:
        raise AptRefused("Ubuntu source lacks a required Noble suite.")
    return b"".join(output[:-1])


def apt_selectors(
    source: Path, sourceparts: Path, lists: Path, archives: Path
) -> dict[str, str]:
    return {
        "Dir::Etc::sourcelist": str(source),
        "Dir::Etc::sourceparts": str(sourceparts),
        "Dir::State::lists": str(lists),
        "Dir::Cache::archives": str(archives),
        "Dir::Cache::pkgcache": "",
        "Dir::Cache::srcpkgcache": "",
        "APT::Update::Error-Mode": "any",
    }


def apt_options(selectors: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        token
        for name, value in selectors.items()
        for token in ("-o", f"{name}={value}")
    )


def apt_configuration(contents: bytes) -> dict[str, str]:
    fields = (
        line.partition(' "') for line in contents.decode("utf-8", "strict").splitlines()
    )
    return {
        key.lower(): value[:-2]
        for key, boundary, value in fields
        if boundary and value.endswith('";')
    }


def validate_profile(contents: bytes, selectors: Mapping[str, str]) -> None:
    actual = apt_configuration(contents)
    if (
        any(actual.get(key.lower()) != value for key, value in selectors.items())
        or actual.get("rootdir", "") not in {"", "/"}
        or actual.get("dir::state::status") != "/var/lib/dpkg/status"
        or any(
            (key == "apt::sources::with" and value)
            or key.startswith("apt::sources::with::")
            for key, value in actual.items()
        )
        or any(
            actual.get(key, "false").lower() not in FALSE_VALUES
            for key in FALSE_SETTINGS
        )
        or any(
            actual.get(key, "true").lower() not in TRUE_VALUES for key in TRUE_SETTINGS
        )
    ):
        raise AptRefused("Effective apt-get inputs differ from the Noble profile.")


async def require_profile(
    selectors: Mapping[str, str], environment: dict[str, str]
) -> None:
    process = await asyncio.create_subprocess_exec(
        APT_GET,
        *apt_options(selectors),
        "dump",
        executable=APT_CONFIG,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        env=environment,
    )
    if process.stdout is None:
        raise AptRefused("Effective apt-get profile output is unavailable.")
    try:
        await process.stdout.readexactly(PROFILE_LIMIT + 1)
    except asyncio.IncompleteReadError as complete:
        if await process.wait():
            raise AptRefused("Effective apt-get profile could not be read.") from None
        validate_profile(complete.partial, selectors)
    else:
        process.kill()
        await process.wait()
        raise AptRefused("Effective apt-get profile exceeds its read bound.")


class Stage(NamedTuple):
    service: str

    @property
    def path(self) -> Path:
        if not SERVICE_NAME.fullmatch(self.service):
            raise AptRefused("Ubuntu package service name is invalid.")
        return RUNTIME / self.service

    @property
    def signer(self) -> Path:
        return self.path / SIGNER.name

    @property
    def adapter(self) -> Path:
        return self.path / "bin" / "apt-get"

    @property
    def refusal(self) -> Path:
        return self.path / "refused"

    @property
    def native_failure(self) -> Path:
        return self.path / "native-failure"

    @property
    def selectors(self) -> dict[str, str]:
        return apt_selectors(
            self.path / SOURCE.name,
            self.path / "sourceparts",
            self.path / "lists",
            self.path / "archives",
        )

    @property
    def options(self) -> tuple[str, ...]:
        return apt_options(self.selectors)

    def invocation_record(self, invocation: str) -> bytes:
        if not INVOCATION.fullmatch(invocation) or invocation == "0" * 32:
            raise AptRefused("Ubuntu package service invocation is invalid.")
        return invocation.encode("ascii") + b"\n"

    def require_invocation(self, invocation: str) -> None:
        record = read_input(self.path / "invocation", 33, "Package stage invocation")
        if record != self.invocation_record(invocation):
            raise AptRefused("Ubuntu package stage belongs to a different invocation.")

    def require_process(self) -> None:
        if Path("/proc/self/cgroup").read_bytes() != self.cgroup_record():
            raise AptRefused("Ubuntu package process is outside the original service.")

    def cgroup_record(self) -> bytes:
        return f"0::/system.slice/{self.path.name}\n".encode("ascii")

    @property
    def cgroup(self) -> Path:
        return Path("/sys/fs/cgroup/system.slice") / self.path.name

    def cgroup_identity(self) -> bytes:
        status = self.cgroup.stat(follow_symlinks=False)
        check_directory(status, "Original native package cgroup")
        return f"{status.st_dev} {status.st_ino}\n".encode("ascii")

    def prepare(self, invocation: str) -> None:
        payload = self.invocation_record(invocation)
        check_directory(RUNTIME.parent.stat(follow_symlinks=False), "Package runtime")
        RUNTIME.mkdir(mode=0o755, exist_ok=True)
        check_directory(RUNTIME.stat(follow_symlinks=False), "Package stage root")
        self.path.mkdir(mode=0o755)
        (self.path / "invocation").write_bytes(payload)
        (self.path / "cgroup").write_bytes(self.cgroup_identity())
        signer = read_signer()
        mirrors = self.path / MIRRORS.name
        source = rewrite_sources(
            read_input(SOURCE, SOURCE_LIMIT, "Noble source input"), self.signer, mirrors
        )
        self.signer.write_bytes(signer)
        mirrors.write_bytes(MIRROR)
        (self.path / SOURCE.name).write_bytes(source)
        for directory_name in ("sourceparts", "lists", "archives", "bin"):
            (self.path / directory_name).mkdir(mode=0o755)
        self.adapter.with_name("sh").symlink_to("/bin/sh")
        command = shlex.join(
            (PYTHON, "-I", str(Path(__file__).resolve()), "apt", self.service)
        )
        self.adapter.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="ascii")
        self.adapter.chmod(0o755)

    def cleanup(self, invocation: str, cgroup: bytes) -> None:
        try:
            status = self.path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        check_directory(status, "Package stage")
        self.require_invocation(invocation)
        if read_input(self.path / "cgroup", 64, "Package stage cgroup") != cgroup:
            raise AptRefused("Ubuntu package stage lacks the original cgroup proof.")
        try:
            original = self.cgroup_identity()
        except FileNotFoundError:
            original = None
        if original is not None and (
            original != cgroup
            or b"populated 0"
            not in (self.cgroup / "cgroup.events").read_bytes().splitlines()
        ):
            raise AptRefused("The original Ubuntu package stage is still in use.")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise AptRefused("Ubuntu package stage removal cannot keep ownership.")
        shutil.rmtree(self.path)


def apt_operation(arguments: Sequence[str]) -> str:
    if tuple(arguments) == ("update",):
        return "update"
    packages = tuple(arguments[len(INSTALL) :])
    if (
        tuple(arguments[: len(INSTALL)]) != INSTALL
        or not 1 <= len(packages) <= 128
        or not all(PACKAGE.fullmatch(package) for package in packages)
    ):
        raise AptRefused("The package adapter refused the APT command.")
    return "console" if packages == CONSOLE_PACKAGES else "install"


async def execute(command: Sequence[str], environment: dict[str, str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=sys.stderr,
        env=environment,
        cwd=ROOT,
    )
    return await process.wait()


def receipt(stage: Stage, operation: str) -> Path:
    return stage.path / f"{operation}.ok"


async def guarded_apt(stage: Stage, arguments: Sequence[str]) -> int:
    stage.require_process()
    stage.require_invocation(os.environ.get("INVOCATION_ID", ""))
    try:
        operation = apt_operation(arguments)
        predecessor = {"update": None, "install": "update", "console": "install"}[
            operation
        ]
        if (
            stage.refusal.exists()
            or stage.native_failure.exists()
            or receipt(stage, operation).exists()
            or (predecessor is not None and not receipt(stage, predecessor).is_file())
        ):
            raise AptRefused("The package adapter refused the APT command order.")
        await require_profile(stage.selectors, APT_ENV)
        sys.stderr.write(f"Console Noble APT {operation}: {' '.join(arguments)}\n")
        result = await execute((APT_GET, *stage.options, *arguments), APT_ENV)
        if result == 0:
            receipt(stage, operation).touch(exist_ok=False)
        elif result == NATIVE_APT_ERROR:
            stage.native_failure.touch(exist_ok=False)
        else:
            raise AptRefused("Native APT returned an unexpected status.")
        return result
    except (AptRefused, OSError, UnicodeError):
        stage.refusal.touch()
        raise


def playwright_outcome(status: int, *, native_failure: bool, refusal: bool) -> int:
    if not refusal and (status, native_failure) in {(0, False), (1, True)}:
        return status
    return 78


async def install(
    stage: Stage, invocation: str, node: str, cli: str, operation: str
) -> int:
    stage.require_process()
    stage.prepare(invocation)
    await require_profile(stage.selectors, APT_ENV)
    if operation == "refusal-witness":
        receipt(stage, "update").touch(exist_ok=False)
    elif operation == "unavailable-adapter-witness":
        stage.adapter.chmod(0o644)
    else:
        recovery = await execute((DPKG, "--configure", "-a"), MANAGER_ENV)
        if recovery:
            return recovery
    playwright_env = {
        **MANAGER_ENV,
        "INVOCATION_ID": invocation,
        "PATH": str(stage.adapter.parent),
    }
    dependencies = await execute(
        (node, cli, "install-deps", "chromium-headless-shell"), playwright_env
    )
    stage.require_invocation(invocation)
    if operation != "install" and (
        dependencies != 1
        or stage.refusal.is_file() != (operation == "refusal-witness")
        or stage.native_failure.is_file()
    ):
        return 1
    outcome = playwright_outcome(
        dependencies,
        native_failure=stage.native_failure.is_file(),
        refusal=stage.refusal.exists(),
    )
    if outcome == 78:
        sys.stderr.write("The package adapter refused Playwright acquisition.\n")
    if outcome:
        return outcome
    if not receipt(stage, "install").is_file():
        raise AptRefused("Playwright did not complete both guarded APT commands.")
    return await execute(
        (str(stage.adapter), *INSTALL, *CONSOLE_PACKAGES), playwright_env
    )


async def main(arguments: Sequence[str]) -> int:
    if os.geteuid() != 0 or len(arguments) < 2:
        return 78
    operation, service, *values = arguments
    stage = Stage(service)
    try:
        if operation == "apt":
            return await guarded_apt(stage, values)
        if operation in PLAYWRIGHT_WORK and len(values) == 3:
            return await install(stage, values[0], values[1], values[2], operation)
        if operation == "prepare" and len(values) == 1:
            stage.require_process()
            stage.prepare(values[0])
            return 0
        if operation == "cleanup" and len(values) == 3:
            invocation, device, inode = values
            if any(not re.fullmatch(r"[0-9]{1,20}", item) for item in (device, inode)):
                return 78
            stage.cleanup(invocation, f"{device} {inode}\n".encode("ascii"))
            return 0
        return 78
    except AptRefused as refusal:
        sys.stderr.write(f"{refusal}\n")
        return 1 if operation in PLAYWRIGHT_WITNESSES else 78
    except (OSError, UnicodeError):
        sys.stderr.write("Console Noble APT ownership or input access failed.\n")
        return 1 if operation in PLAYWRIGHT_WITNESSES else 78


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
