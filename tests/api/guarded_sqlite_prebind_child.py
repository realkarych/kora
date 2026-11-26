import asyncio
import errno
import os
import re
import select
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, PositiveInt, ValidationError
from sqlalchemy import event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool.base import ConnectionPoolEntry

from kora.api.serving import sqlite_schema_ready_for_serving
from kora.db.connection import (
    Database,
    DatabaseClosedError,
    DatabaseWriterRejected,
    SqliteDriverConnection,
)
from kora.db.run_event_page_v1 import SQLITE_EVENT_PAGE_INDEX

API_UID: int = 65532
OTHER_UID: int = 65533
BINDING_LIMIT: int = 2048
WITNESS_TIMEOUT: float = 20.0
CAPABILITY_FIELDS: tuple[str, ...] = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
WITNESS_MODULE: str = "tests.api.guarded_sqlite_prebind_child"
OWNED_GUEST_RELEASE: str = "6.8.0-147-generic"
FILESYSTEM_BYTES: int = 32 * 1024 * 1024
SCHEMA_READ = text(
    "SELECT version_num FROM alembic_version WHERE kora_prebind_witness_barrier()"
)

FileIdentity = tuple[int, int]


class WitnessCase(StrEnum):
    SCHEMA = "schema"
    INEXACT = "inexact"
    ABSENT = "absent"
    FOREIGN = "foreign"
    MISSING_FILE = "missing_file"


def require(observed: bool, reason: str) -> None:
    if not observed:
        raise DatabaseClosedError(reason)


def identity(info: os.stat_result) -> FileIdentity:
    return info.st_dev, info.st_ino


def native_identity(path: Path) -> FileIdentity:
    return identity(path.lstat())


def process_start(process: int) -> int:
    return int(Path(f"/proc/{process}/stat").read_text().rsplit(")", 1)[1].split()[19])


def mount_namespace() -> int:
    return Path("/proc/self/ns/mnt").stat().st_ino


def boot_identity() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_status(process: int | str) -> dict[str, str]:
    return dict(
        line.split(":", 1)
        for line in Path(f"/proc/{process}/status").read_text().splitlines()
    )


def owned_mounts(domain: Path, main: Path) -> tuple[int, int]:
    mounts = [
        line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()
    ]
    roots = [row for row in mounts if row[4] == str(domain)]
    targets = [row for row in mounts if row[4] == str(main)]
    require(len(roots) == len(targets) == 1, "the protected mounts are unavailable")
    root, target = roots[0], targets[0]
    require(
        root[0] != target[0]
        and root[2] == target[2]
        and root[3] == "/"
        and target[3] == "/backing/main.db"
        and {row[0] for row in mounts if row[2] == root[2]} == {root[0], target[0]}
        and all(
            row[6:8] == ["-", "ext4"] and "rw" in row[5].split(",")
            for row in (root, target)
        ),
        "the private ext4 file binding changed",
    )
    return int(root[0]), int(target[0])


class LaunchBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    launcher: PositiveInt
    launcher_start: PositiveInt
    boot: str
    namespace: PositiveInt
    mounts: tuple[int, int]
    directory: str
    ancestors: tuple[FileIdentity, ...]
    main: FileIdentity


def protected_directories(domain: Path) -> tuple[Path, ...]:
    return (
        *reversed(domain.parents),
        domain,
        domain / "backing",
        domain / "service",
        domain / "service" / "api",
    )


def assert_child_credentials() -> None:
    fields = process_status("self")
    require(
        sys.platform == "linux"
        and os.getuid() == os.geteuid() == API_UID
        and os.getgid() == os.getegid() == API_UID
        and not os.getgroups()
        and all(int(fields[key].strip(), 16) == 0 for key in CAPABILITY_FIELDS)
        and fields["NoNewPrivs"].strip() == "1"
        and {name for _, name in socket.if_nameindex()} == {"lo"},
        "the checked child lacks the disposable guest identity",
    )


@dataclass(frozen=True)
class ProtectedRead:
    binding: LaunchBinding
    domain: Path
    main: Path
    parent_fd: int
    lifetime_fd: int

    def require_live(self) -> None:
        poll = select.poll()
        poll.register(self.parent_fd, select.POLLIN)
        poll.register(self.lifetime_fd, select.POLLIN)
        expected = self.binding
        require(
            not poll.poll(0)
            and os.getppid() == expected.launcher
            and process_start(expected.launcher) == expected.launcher_start
            and process_status(expected.launcher)["Uid"].split() == ["0"] * 4
            and boot_identity() == expected.boot
            and mount_namespace() == expected.namespace,
            "the original SQLite read scope was withdrawn",
        )
        current = self.main.lstat()
        parents = tuple(path.lstat() for path in protected_directories(self.domain))
        require(
            tuple(map(identity, parents)) == expected.ancestors
            and all(stat.S_ISDIR(info.st_mode) for info in parents)
            and all(info.st_uid == 0 and not info.st_mode & 0o022 for info in parents)
            and parents[-1].st_gid == API_UID
            and stat.S_IMODE(parents[-1].st_mode) == 0o750
            and stat.S_IMODE(parents[-3].st_mode) == 0o700
            and owned_mounts(self.domain, self.main) == expected.mounts,
            "the protected SQLite parents or mount changed",
        )
        require(
            identity(current) == expected.main
            and stat.S_ISREG(current.st_mode)
            and current.st_nlink == 1
            and current.st_uid == API_UID
            and stat.S_IMODE(current.st_mode) == 0o600,
            "the protected SQLite main changed",
        )


@contextmanager
def consume_binding(
    supplied_fd: int, lifetime_fd: int, case: WitnessCase
) -> Generator[ProtectedRead, None, None]:
    with os.fdopen(supplied_fd, "rb") as source:
        data = source.read(BINDING_LIMIT + 1)
    require(
        0 < len(data) <= BINDING_LIMIT,
        "the private SQLite startup binding is unavailable",
    )
    try:
        binding = LaunchBinding.model_validate_json(data)
    except ValidationError as error:
        raise DatabaseClosedError(
            "the private SQLite startup binding is invalid"
        ) from error
    assert_child_credentials()
    require(
        binding.launcher == os.getppid(),
        "the launch binding belongs to a different parent",
    )
    domain = Path(binding.directory)
    require(
        domain.is_absolute() and ".." not in domain.parts,
        "the protected SQLite directory is inexact",
    )
    main = domain / "service" / "api" / "main.db"
    if case is WitnessCase.MISSING_FILE:
        main = main.with_name("missing.db")
    os.set_inheritable(lifetime_fd, False)
    if sys.platform == "linux":
        parent_fd = os.pidfd_open(binding.launcher)
        try:
            yield ProtectedRead(binding, domain, main, parent_fd, lifetime_fd)
        finally:
            os.close(parent_fd)
    else:
        raise DatabaseClosedError("the protected read requires Linux")


def held_main_descriptors(main: FileIdentity) -> tuple[int, ...]:
    matches: list[int] = []
    for name in os.listdir("/proc/self/fd"):
        descriptor = int(name)
        try:
            info = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            if identity(info) == main:
                matches.append(descriptor)
    return tuple(matches)


def native_access_mode(descriptor: int) -> int:
    fields = dict(
        line.split(":", 1)
        for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines()
    )
    return int(fields["flags"].strip(), 8) & os.O_ACCMODE


@dataclass
class ReadObservations:
    native_main: FileIdentity | None = None
    workers: list[threading.Thread] = field(default_factory=list[threading.Thread])

    def attach(self, database: Database) -> None:
        event.listen(database.reader_engine.pool, "connect", self.opened)

    def opened(self, raw: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        driver: object = record.driver_connection
        assert isinstance(driver, SqliteDriverConnection)
        worker: object = getattr(driver, "_thread", None)
        assert isinstance(worker, threading.Thread)
        assert worker not in self.workers
        assert worker.is_alive()
        if self.native_main is not None:
            descriptors = held_main_descriptors(self.native_main)
            assert len(descriptors) == 1
            assert native_access_mode(descriptors[0]) == os.O_RDONLY
            assert worker.native_id is not None
            assert Path(f"/proc/self/task/{worker.native_id}").is_dir()
        self.workers.append(worker)

    def assert_retired(self, count: int) -> None:
        assert len(self.workers) == count
        assert all(not worker.is_alive() for worker in self.workers)
        if self.native_main is not None:
            assert not held_main_descriptors(self.native_main)
            assert all(
                not Path(f"/proc/self/task/{worker.native_id}").exists()
                for worker in self.workers
            )


async def check_schema(database: Database) -> bool:
    try:
        return await sqlite_schema_ready_for_serving(database)
    finally:
        await database.close_sqlite_serving()


async def refused_writer(database: Database) -> None:
    assert not database.writer_is_free()
    try:
        _writer = database.writer_engine
    except DatabaseClosedError:
        pass
    else:
        raise AssertionError("the guarded reader exposed a writer")
    deadline = asyncio.get_running_loop().time() + WITNESS_TIMEOUT
    for entry in (
        database.transaction(),
        database.transaction(admission_deadline=deadline),
        database.migration_connection(),
    ):
        try:
            async with entry:
                raise AssertionError("the guarded reader admitted a writer")
        except (DatabaseClosedError, DatabaseWriterRejected):
            continue


class BarrierRegistration(Protocol):
    async def create_function(
        self, name: str, num_params: int, func: Callable[[], int]
    ) -> None: ...


async def check_withdrawal(
    database: Database, withdraw: Callable[[], None], observations: ReadObservations
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    leave = threading.Event()

    def native_barrier() -> int:
        loop.call_soon_threadsafe(entered.set)
        if not leave.wait(WITNESS_TIMEOUT):
            raise TimeoutError("the disposable read barrier was not released")
        return 1

    async def blocked_read(connection: AsyncConnection) -> object:
        checked_out = await connection.get_raw_connection()
        driver: object = checked_out.driver_connection
        assert isinstance(driver, SqliteDriverConnection)
        await cast(BarrierRegistration, driver).create_function(
            "kora_prebind_witness_barrier", 0, native_barrier
        )
        value = await connection.scalar(SCHEMA_READ)
        assert isinstance(value, str)
        try:
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
        except DatabaseClosedError:
            return value
        raise AssertionError(
            "the original connection accepted a new statement after withdrawal"
        )

    async with asyncio.timeout(WITNESS_TIMEOUT):
        read = asyncio.create_task(database.read(blocked_read))
        try:
            await entered.wait()
            await asyncio.to_thread(withdraw)
            assert not read.done()
            try:
                await sqlite_schema_ready_for_serving(database)
            except DatabaseClosedError:
                pass
            else:
                raise AssertionError("the withdrawn scope admitted a second read")
            assert len(observations.workers) == 1
        finally:
            leave.set()
            try:
                await read
            except DatabaseClosedError:
                pass
            else:
                raise AssertionError("the withdrawn scope published a successful read")
            finally:
                await database.close_sqlite_serving()
    observations.assert_retired(1)


@dataclass
class NativeAttempts:
    database_opens: int = 0
    socket_binds: int = 0

    def audit(self, name: str, args: tuple[object, ...]) -> None:
        if name == "sqlite3.connect":
            self.database_opens += 1
        if name == "socket.bind":
            self.socket_binds += 1


def exchange_withdrawal(lifetime_fd: int) -> None:
    sys.stdout.write("PREBIND WITHDRAW\n")
    sys.stdout.flush()
    poll = select.poll()
    poll.register(lifetime_fd, select.POLLIN)
    assert poll.poll(int(WITNESS_TIMEOUT * 1000)) == [(lifetime_fd, select.POLLHUP)]


async def run_protected_case(scope: ProtectedRead, case: WitnessCase) -> None:
    observation = ReadObservations(scope.binding.main)
    database = Database.guarded_sqlite_reader(scope.main, scope.require_live)
    observation.attach(database)
    await refused_writer(database)
    assert not observation.workers
    expected_ready = case is WitnessCase.SCHEMA
    assert await check_schema(database) == expected_ready
    observation.assert_retired(2)
    if expected_ready:
        withdrawal = Database.guarded_sqlite_reader(scope.main, scope.require_live)
        cancelled = ReadObservations(scope.binding.main)
        cancelled.attach(withdrawal)
        await check_withdrawal(
            withdrawal, lambda: exchange_withdrawal(scope.lifetime_fd), cancelled
        )


async def run_child(supplied_fd: int, lifetime_fd: int, case: WitnessCase) -> None:
    attempts = NativeAttempts()
    sys.addaudithook(attempts.audit)
    try:
        with consume_binding(supplied_fd, lifetime_fd, case) as scope:
            await run_protected_case(scope, case)
    except (DatabaseClosedError, FileNotFoundError):
        if case not in {
            WitnessCase.ABSENT,
            WitnessCase.FOREIGN,
            WitnessCase.MISSING_FILE,
        }:
            raise
        assert attempts.database_opens == 0
        assert attempts.socket_binds == 0
        sys.stdout.write(f"PREBIND REFUSED {case.value} opens=0 binds=0\n")
    else:
        assert case in {WitnessCase.SCHEMA, WitnessCase.INEXACT}
        assert attempts.database_opens == (3 if case is WitnessCase.SCHEMA else 2)
        assert attempts.socket_binds == 0
        sys.stdout.write(
            f"PREBIND SETTLED {case.value} opens={attempts.database_opens} "
            "binds=0 fds=0 workers=0\n"
        )
    finally:
        os.close(lifetime_fd)


def fixture_message(message: str) -> None:
    sys.stdout.write(f"PREBIND {message}\n")
    sys.stdout.flush()


def fixture_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "PYTHONPATH": os.environ["PYTHONPATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def command(*args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        args,
        env=fixture_environment() if env is None else env,
        stdout=subprocess.PIPE,
        check=True,
        timeout=WITNESS_TIMEOUT * 2,
        text=True,
    ).stdout


def unprivileged(uid: int, *args: str) -> tuple[str, ...]:
    return (
        "/usr/bin/setpriv",
        f"--reuid={uid}",
        f"--regid={uid}",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
        sys.executable,
        "-B",
        "-m",
        WITNESS_MODULE,
        *args,
    )


def require_owned_guest() -> None:
    info = os.uname()
    require(
        sys.platform == "linux"
        and info.machine == "x86_64"
        and info.release == OWNED_GUEST_RELEASE
        and os.getuid() == os.geteuid() == 0
        and {name for _, name in socket.if_nameindex()} == {"lo"}
        and Path("/sys/class/dmi/id/sys_vendor").read_text().strip() == "QEMU",
        "the owned offline Ubuntu guest is required",
    )


def assert_other_uid_refused(*paths: str) -> None:
    assert os.getuid() == os.geteuid() == OTHER_UID
    for name in paths:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EPERM}:
                raise
        else:
            os.close(descriptor)
            raise AssertionError(
                "a different guest identity could open protected storage"
            )


def run_bound_child(binding: LaunchBinding, case: WitnessCase) -> None:
    with ExitStack() as owned:
        startup_read, startup_write = os.pipe()
        owned.callback(os.close, startup_read)
        startup = owned.enter_context(os.fdopen(startup_write, "wb"))
        scope_read, scope_write = os.pipe()
        owned.callback(os.close, scope_read)
        scope = owned.enter_context(os.fdopen(scope_write, "wb"))
        child = owned.enter_context(
            subprocess.Popen(
                unprivileged(
                    API_UID, "child", str(startup_read), str(scope_read), case.value
                ),
                env=fixture_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                pass_fds=(startup_read, scope_read),
            )
        )
        try:
            if case is not WitnessCase.ABSENT:
                startup.write(binding.model_dump_json().encode("ascii"))
            startup.close()
            assert child.stdout is not None
            if case is WitnessCase.SCHEMA:
                readable, _, _ = select.select([child.stdout], [], [], WITNESS_TIMEOUT)
                assert readable
                assert child.stdout.readline(128) == b"PREBIND WITHDRAW\n"
                scope.close()
            output, _ = child.communicate(timeout=WITNESS_TIMEOUT)
            assert child.returncode == 0
            assert len(output) < BINDING_LIMIT
            result = (
                "SETTLED"
                if case in {WitnessCase.SCHEMA, WitnessCase.INEXACT}
                else "REFUSED"
            )
            assert output.startswith(f"PREBIND {result} {case.value} ".encode("ascii"))
            sys.stdout.write(output.decode("ascii"))
            sys.stdout.flush()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=WITNESS_TIMEOUT)
        assert not Path(f"/proc/{child.pid}").exists()


def prepare_ordinary_main(path: Path, *, inexact: bool) -> bytes:
    env = fixture_environment()
    env["KORA_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    command(sys.executable, "-B", "-m", "kora", "db", "upgrade", env=env)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        if inexact:
            database.execute(f"DROP INDEX {SQLITE_EVENT_PAGE_INDEX}").close()
    path.chmod(0o600)
    os.chown(path, API_UID, API_UID)
    assert list(path.parent.iterdir()) == [path]
    return path.read_bytes()


def open_launch_anchor(path: Path) -> int:
    if sys.platform == "linux":
        return os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    raise DatabaseClosedError("the protected read requires Linux")


def witness_bound_image(domain: Path, device: str, *, inexact: bool) -> None:
    backing = domain / "backing" / "main.db"
    main = domain / "service" / "api" / "main.db"
    for directory in (domain, main.parent.parent):
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chown(directory, 0, API_UID)
        directory.chmod(0o710)
    backing.parent.mkdir(mode=0o700)
    main.parent.mkdir(mode=0o700)
    os.chown(main.parent, 0, API_UID)
    main.parent.chmod(0o750)
    ordinary = prepare_ordinary_main(backing, inexact=inexact)
    main.touch(mode=0o600, exist_ok=False)
    parents = protected_directories(domain)
    with ExitStack() as held:
        command("/usr/bin/mount", "--bind", str(backing), str(main))
        held.callback(command, "/usr/bin/umount", str(main))
        anchors: list[int] = []
        for path in (*parents, backing):
            fd = open_launch_anchor(path)
            held.callback(os.close, fd)
            anchors.append(fd)
        facts = tuple(identity(os.fstat(fd)) for fd in anchors)
        assert native_identity(main) == facts[-1]
        binding = LaunchBinding(
            launcher=os.getpid(),
            launcher_start=process_start(os.getpid()),
            boot=boot_identity(),
            namespace=mount_namespace(),
            mounts=owned_mounts(domain, main),
            directory=str(domain),
            ancestors=facts[:-1],
            main=facts[-1],
        )
        command(
            *unprivileged(OTHER_UID, "refuse_access", str(main), str(backing), device)
        )
        if inexact:
            run_bound_child(binding, WitnessCase.INEXACT)
        else:
            run_bound_child(binding, WitnessCase.ABSENT)
            foreign = binding.model_copy(update={"launcher": os.getppid()})
            run_bound_child(foreign, WitnessCase.FOREIGN)
            run_bound_child(binding, WitnessCase.MISSING_FILE)
            run_bound_child(binding, WitnessCase.SCHEMA)
        assert tuple(identity(os.fstat(fd)) for fd in anchors) == facts
        assert tuple(native_identity(path) for path in (*parents, backing)) == facts
        assert native_identity(main) == binding.main
        assert owned_mounts(domain, main) == binding.mounts
        assert main.read_bytes() == ordinary
        assert list(main.parent.iterdir()) == [main]
        assert list(backing.parent.iterdir()) == [backing]
        fixture_message("ANCHOR reaped=1 exact_main=1 sidecars=0 other_uid_refused=1")
    main.unlink()
    backing.unlink()
    for directory in (main.parent, main.parent.parent, backing.parent):
        directory.rmdir()


def release_loop(device: str, indicator: Path) -> None:
    command("/usr/sbin/losetup", "--detach", device)
    assert not indicator.exists()


def run_launcher(base: Path, outer_namespace: int) -> None:
    require_owned_guest()
    require(
        mount_namespace() != outer_namespace, "a private mount namespace is required"
    )
    domain = base / "fs"
    image = base / "media" / "owned.ext4"
    command("/usr/sbin/modprobe", "loop")
    with ExitStack() as owned:
        with image.open("xb") as raw:
            raw.truncate(FILESYSTEM_BYTES)
        owned.callback(image.unlink)
        command("/usr/sbin/mke2fs", "-q", "-F", "-t", "ext4", str(image))
        device = command("/usr/sbin/losetup", "--find", "--show", str(image)).strip()
        require(
            re.fullmatch(r"/dev/loop[0-9]+", device) is not None,
            "the loop root is unknown",
        )
        indicator = Path(f"/sys/class/block/{Path(device).name}/loop/backing_file")
        require(
            indicator.read_text().strip() == str(image), "the loop image is not owned"
        )
        owned.callback(release_loop, device, indicator)
        command(
            "/usr/bin/mount",
            "-t",
            "ext4",
            "-o",
            "nosuid,nodev,noexec",
            device,
            str(domain),
        )
        owned.callback(command, "/usr/bin/umount", str(domain))
        (base / "media" / "mounted").touch(exist_ok=False)
        end = time.monotonic() + WITNESS_TIMEOUT
        while not (base / "media" / "checked").exists():
            require(time.monotonic() < end, "the outside observer did not settle")
            time.sleep(0.025)
        for inexact in (False, True):
            witness_bound_image(domain, device, inexact=inexact)
    assert not image.exists()
    fixture_message("RELEASE bound_mounts=0 devices=0 image=0")


def run_owned_guest() -> None:
    require_owned_guest()
    version = ".".join(map(str, sys.version_info[:3]))
    fixture_message(
        f"ENV Linux-x86_64 {OWNED_GUEST_RELEASE} "
        f"Python={version} SQLite={sqlite3.sqlite_version}"
    )
    with tempfile.TemporaryDirectory(prefix="kora-1882-", dir="/run") as scratch:
        base = Path(scratch)
        os.chown(base, 0, API_UID)
        base.chmod(0o710)
        (base / "media").mkdir(mode=0o700)
        domain = base / "fs"
        domain.mkdir(mode=0o700)
        with subprocess.Popen(
            (
                "/usr/bin/unshare",
                "--mount",
                "--propagation",
                "private",
                sys.executable,
                "-B",
                "-m",
                WITNESS_MODULE,
                "launcher",
                str(base),
                str(mount_namespace()),
            ),
            env=fixture_environment(),
            stdin=subprocess.DEVNULL,
        ) as launcher:
            try:
                end = time.monotonic() + WITNESS_TIMEOUT
                while not (base / "media" / "mounted").exists():
                    require(
                        launcher.poll() is None and time.monotonic() < end,
                        "the guest launcher did not establish its private mount",
                    )
                    time.sleep(0.025)
                assert not list(domain.iterdir())
                assert (
                    Path(f"/proc/{launcher.pid}/ns/mnt").stat().st_ino
                    != mount_namespace()
                )
                (base / "media" / "checked").touch(exist_ok=False)
                assert launcher.wait(timeout=100) == 0
            finally:
                if launcher.poll() is None:
                    (base / "media" / "checked").touch()
                    launcher.wait(timeout=100)
            assert not Path(f"/proc/{launcher.pid}").exists()
            assert not list(domain.iterdir())
    assert not base.exists()
    fixture_message("PASS launcher_reaped=1 directory=0")


if __name__ == "__main__":
    role, *words = sys.argv[1:]
    os.umask(0o077)
    if role == "child":
        binding_fd, scope_fd, witness = words
        asyncio.run(run_child(int(binding_fd), int(scope_fd), WitnessCase(witness)))
    elif role == "refuse_access":
        assert_other_uid_refused(*words)
    elif role == "launcher":
        root, namespace = words
        run_launcher(Path(root), int(namespace))
    elif role == "guest" and not words:
        run_owned_guest()
    else:
        raise ValueError("unknown protected SQLite fixture role")
