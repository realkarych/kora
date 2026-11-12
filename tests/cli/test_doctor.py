import asyncio
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.engine import URL

from kora.__main__ import DoctorSettings, main, run_interruptible
from kora.cel_eval.binding import PINNED_VERSION
from kora.db.connection import DatabaseSettings
from kora.plugins.runtime.local.runtime import default_state_root
from kora.plugins.runtime.local.state_probe import PROBE_PREFIX
from tests.cel_eval.children import isolated_path, isolated_site, run_child
from tests.support.databases import DatabaseKind, PostgresServer

SHORT_DEADLINE_SECONDS: float = 0.5
EVENT_DEADLINE_SECONDS: float = 30.0
PROCESS_DEADLINE_SECONDS: float = 60.0
HIDDEN_MARKER: str = "PASSWORD-MARKER-5513"
ENVIRONMENT_MARKER: str = "ENVIRONMENT-MARKER-8831"
UNRELATED_VARIABLE: str = "KORA_DOCTOR_TEST_UNRELATED"
CHECKS: tuple[str, ...] = ("version", "cel", "database", "state_root")
UNSAFE: str = "state_root_unsafe"


def url_text(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def postgresql_url(port: int, password: str = HIDDEN_MARKER) -> str:
    return f"postgresql+asyncpg://kora:{password}@127.0.0.1:{port}/kora"


@dataclass(frozen=True)
class Installation:
    home: Path
    root: Path

    def settings(self, deadline: float = SHORT_DEADLINE_SECONDS) -> DoctorSettings:
        return DoctorSettings(database_deadline_seconds=deadline, home=self.home_path)

    def home_path(self) -> Path:
        return self.home

    def entries(self) -> list[str]:
        return sorted(os.listdir(self.root)) if self.root.is_dir() else []


@pytest.fixture
def installation(tmp_path: Path) -> Installation:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    root = default_state_root(sys.platform, {}, home)
    root.mkdir(parents=True)
    root.chmod(0o700)
    return Installation(home, root)


@dataclass
class Listener:
    answers: bool
    endpoint: socket.socket = field(init=False)
    accepted: threading.Event = field(default_factory=threading.Event)
    connections: list[socket.socket] = field(default_factory=lambda: [])

    def __post_init__(self) -> None:
        self.endpoint = socket.create_server(("127.0.0.1", 0))
        threading.Thread(target=self.serve, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.endpoint.getsockname()[1])

    def serve(self) -> None:
        while True:
            try:
                connection, _ = self.endpoint.accept()
            except OSError:
                return
            if self.answers:
                connection.close()
            else:
                self.connections.append(connection)
            self.accepted.set()

    def peer_closed(self) -> bool:
        connection = self.connections[0]
        connection.settimeout(EVENT_DEADLINE_SECONDS)
        while connection.recv(4096):
            continue
        return True

    def close(self) -> None:
        self.endpoint.close()
        for connection in self.connections:
            connection.close()


@pytest.fixture
def silent_listener() -> Iterator[Listener]:
    created = Listener(answers=False)
    yield created
    created.close()


@pytest.fixture
def closing_listener() -> Iterator[Listener]:
    created = Listener(answers=True)
    yield created
    created.close()


def closed_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as endpoint:
        return int(endpoint.getsockname()[1])


def run_doctor(
    environment: dict[str, str],
    settings: DoctorSettings,
    capfd: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object], str]:
    code = main(["doctor", "--json"], environment, doctor_settings=settings)
    captured = capfd.readouterr()
    return code, cast(dict[str, object], json.loads(captured.out)), captured.err


def statuses(report: dict[str, object]) -> dict[str, tuple[object, object]]:
    checks = cast(list[dict[str, object]], report["checks"])
    return {str(check["check"]): (check["status"], check["reason"]) for check in checks}


def detail(report: dict[str, object], name: str) -> object:
    checks = cast(list[dict[str, object]], report["checks"])
    return next(check["detail"] for check in checks if check["check"] == name)


def test_doctor_reports_local_prerequisites(
    database_kind: DatabaseKind,
    database_settings: DatabaseSettings,
    installation: Installation,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    url = url_text(database_settings.url)
    before = sorted(os.listdir(tmp_path))

    code, report, _ = run_doctor(
        {"KORA_DATABASE_URL": url}, installation.settings(), capfd
    )

    assert code == 0
    assert report["status"] == "passed"
    assert statuses(report) == {name: ("passed", None) for name in CHECKS}
    assert detail(report, "database") == database_kind.value
    assert detail(report, "state_root") == str(installation.root)
    assert detail(report, "cel") == PINNED_VERSION
    assert sorted(os.listdir(tmp_path)) == before
    assert installation.entries() == []


@dataclass(frozen=True)
class DatabaseCase:
    name: str
    url: Callable[[Path, Listener, Listener], str]
    reason: str


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def fifo_url(directory: Path) -> str:
    path = directory / "fifo.db"
    os.mkfifo(path)
    return sqlite_url(path)


def text_url(directory: Path) -> str:
    path = directory / "text.db"
    path.write_text("not a database", encoding="utf-8")
    return sqlite_url(path)


def symlink_url(directory: Path) -> str:
    target = directory / "target.db"
    target.write_bytes(b"SQLite format 3\x00")
    path = directory / "link.db"
    path.symlink_to(target)
    return sqlite_url(path)


def unreadable_url(directory: Path) -> str:
    path = directory / "unreadable.db"
    path.write_bytes(b"SQLite format 3\x00")
    path.chmod(0)
    return sqlite_url(path)


DATABASE_CASES: list[DatabaseCase] = [
    DatabaseCase("unset", lambda directory, silent, closing: "", "not_configured"),
    DatabaseCase(
        "invalid",
        lambda directory, silent, closing: (
            f"postgresql+asyncpg://kora:{HIDDEN_MARKER}@127.0.0.1/kora"
        ),
        "invalid_configuration",
    ),
    DatabaseCase(
        "missing",
        lambda directory, silent, closing: sqlite_url(directory / "missing.db"),
        "missing",
    ),
    DatabaseCase(
        "odd_path",
        lambda directory, silent, closing: sqlite_url(directory / "odd%25#x.db"),
        "missing",
    ),
    DatabaseCase(
        "query_in_path",
        lambda directory, silent, closing: sqlite_url(directory / "x.db?mode=rwc"),
        "invalid_configuration",
    ),
    DatabaseCase(
        "fifo",
        lambda directory, silent, closing: fifo_url(directory),
        "not_a_regular_file",
    ),
    DatabaseCase(
        "text", lambda directory, silent, closing: text_url(directory), "not_a_database"
    ),
    DatabaseCase(
        "symlink",
        lambda directory, silent, closing: symlink_url(directory),
        "unreadable",
    ),
    DatabaseCase(
        "unreadable",
        lambda directory, silent, closing: unreadable_url(directory),
        "unreadable",
    ),
    DatabaseCase(
        "closed_port",
        lambda directory, silent, closing: postgresql_url(closed_port()),
        "unreachable",
    ),
    DatabaseCase(
        "connection_closed",
        lambda directory, silent, closing: postgresql_url(closing.port),
        "unreachable",
    ),
    DatabaseCase(
        "silent",
        lambda directory, silent, closing: postgresql_url(silent.port),
        "deadline",
    ),
]


@pytest.mark.parametrize(
    "case", DATABASE_CASES, ids=[case.name for case in DATABASE_CASES]
)
def test_doctor_reports_unusable_database(
    case: DatabaseCase,
    installation: Installation,
    silent_listener: Listener,
    closing_listener: Listener,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    directory = (tmp_path / "databases").resolve()
    directory.mkdir()
    url = case.url(directory, silent_listener, closing_listener)
    before = sorted(os.listdir(directory))

    code, report, err = run_doctor(
        {
            "KORA_DATABASE_URL": url,
            "KORA_LOG_LEVEL": "DEBUG",
            UNRELATED_VARIABLE: ENVIRONMENT_MARKER,
        },
        installation.settings(),
        capfd,
    )

    assert code == 1
    assert statuses(report)["database"] == ("failed", case.reason)
    assert sorted(os.listdir(directory)) == before
    assert all(
        marker not in text
        for marker in (HIDDEN_MARKER, ENVIRONMENT_MARKER)
        for text in (json.dumps(report), err)
    )


def test_doctor_propagates_an_unclassified_driver_failure(
    installation: Installation,
    capfd: pytest.CaptureFixture[str],
) -> None:
    url = postgresql_url(closed_port()) + "?unknown_option=1"

    code = main(
        ["doctor", "--json"],
        {"KORA_DATABASE_URL": url, "KORA_LOG_LEVEL": "DEBUG"},
        doctor_settings=installation.settings(),
    )

    captured = capfd.readouterr()
    assert code == 70
    assert captured.out == ""
    assert "process.failed" in captured.err
    assert HIDDEN_MARKER not in captured.err


def refused_url(
    database_kind: DatabaseKind,
    request: pytest.FixtureRequest,
    directory: Path,
    refusal: str,
) -> tuple[str, str]:
    if database_kind is DatabaseKind.SQLITE:
        if refusal == "credentials":
            return unreadable_url(directory), "unreadable"
        return text_url(directory), "not_a_database"
    server: PostgresServer = request.getfixturevalue("postgres_server")
    if refusal == "credentials":
        return url_text(server.url_for("postgres").set(password=HIDDEN_MARKER)), (
            "rejected"
        )
    return url_text(server.url_for(f"absent_{HIDDEN_MARKER.lower()}")), "rejected"


@pytest.mark.parametrize("refusal", ["credentials", "catalog"])
def test_doctor_reports_refused_database(
    database_kind: DatabaseKind,
    request: pytest.FixtureRequest,
    refusal: str,
    installation: Installation,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    directory = (tmp_path / "databases").resolve()
    directory.mkdir()
    url, reason = refused_url(database_kind, request, directory, refusal)

    code, report, err = run_doctor(
        {"KORA_DATABASE_URL": url, "KORA_LOG_LEVEL": "DEBUG"},
        installation.settings(),
        capfd,
    )

    assert code == 1
    assert statuses(report)["database"] == ("failed", reason)
    assert HIDDEN_MARKER not in json.dumps(report)
    assert HIDDEN_MARKER.lower() not in json.dumps(report)
    assert HIDDEN_MARKER not in err
    assert HIDDEN_MARKER.lower() not in err


DRIVER_MISSING_OUTCOME: str = """
import contextlib, io, json, pathlib
from kora.__main__ import DoctorSettings, main
output = io.StringIO()
with contextlib.redirect_stdout(output):
    code = main(
        ["doctor", "--json"],
        {"KORA_DATABASE_URL": sys.argv[3], "KORA_LOG_LEVEL": "DEBUG"},
        doctor_settings=DoctorSettings(home=lambda: pathlib.Path(sys.argv[2])),
    )
checks = json.loads(output.getvalue())["checks"]
database = next(check for check in checks if check["check"] == "database")
print(code, database["status"], database["reason"])
"""


def is_driver_entry(name: str) -> bool:
    return name == "asyncpg" or name.startswith("asyncpg-")


def test_doctor_reports_missing_postgresql_driver(
    installation: Installation, tmp_path: Path
) -> None:
    site = isolated_site(tmp_path / "site", is_driver_entry)
    url = postgresql_url(closed_port())

    result = run_child(
        DRIVER_MISSING_OUTCOME, isolated_path(site), str(installation.home), url
    )

    assert result.code == 0, result.stderr
    assert result.lines() == ["1 failed driver_missing"]
    assert HIDDEN_MARKER not in result.stderr


@dataclass(frozen=True)
class StateRootCase:
    name: str
    arrange: Callable[[Installation], None]
    reason: str


def remove_root(installation: Installation) -> None:
    installation.root.rmdir()


def widen_root(installation: Installation) -> None:
    installation.root.chmod(0o755)


def link_root(installation: Installation) -> None:
    target = installation.home / "target"
    target.mkdir(mode=0o700)
    installation.root.rmdir()
    installation.root.symlink_to(target, target_is_directory=True)


STATE_ROOT_CASES: list[StateRootCase] = [
    StateRootCase("missing", remove_root, UNSAFE),
    StateRootCase("wrong_mode", widen_root, UNSAFE),
    StateRootCase("symlink", link_root, UNSAFE),
]

if sys.platform == "darwin":

    def freeze_root(installation: Installation) -> None:
        os.chflags(installation.root, stat.UF_IMMUTABLE)

    STATE_ROOT_CASES.append(StateRootCase("immutable", freeze_root, "not_writable"))


def thaw(installation: Installation) -> None:
    root = installation.root
    if sys.platform == "darwin":
        if root.is_dir() and not root.is_symlink():
            os.chflags(root, 0)


def root_state(installation: Installation) -> tuple[bool, bool, int | None]:
    path = installation.root
    exists = os.path.lexists(path)
    return (
        exists,
        path.is_symlink(),
        stat.S_IMODE(path.lstat().st_mode) if exists else None,
    )


@pytest.mark.parametrize(
    "case", STATE_ROOT_CASES, ids=[case.name for case in STATE_ROOT_CASES]
)
def test_doctor_reports_unusable_state_root(
    case: StateRootCase,
    installation: Installation,
    capfd: pytest.CaptureFixture[str],
) -> None:
    case.arrange(installation)
    before = root_state(installation)

    try:
        code, report, _ = run_doctor({}, installation.settings(), capfd)
        after = root_state(installation)
        entries = installation.entries()
    finally:
        thaw(installation)

    assert code == 1
    assert statuses(report)["state_root"] == ("failed", case.reason)
    assert detail(report, "state_root") is None
    assert after == before
    assert not [entry for entry in entries if entry.startswith(PROBE_PREFIX)]


def kora_process(
    arguments: list[str], environment: dict[str, str]
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "kora", *arguments],
        env={"PATH": os.environ["PATH"], **environment},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def finish(process: subprocess.Popen[bytes]) -> tuple[int, bytes, bytes]:
    out, err = process.communicate(timeout=PROCESS_DEADLINE_SECONDS)
    return process.returncode, out, err


def process_environment(installation: Installation, url: str) -> dict[str, str]:
    return {
        "HOME": str(installation.home),
        "KORA_DATABASE_URL": url,
        "KORA_LOG_LEVEL": "DEBUG",
        UNRELATED_VARIABLE: ENVIRONMENT_MARKER,
    }


def stream_texts(out: bytes, err: bytes) -> tuple[str, str]:
    return out.decode("utf-8"), err.decode("utf-8")


def password_of(settings: DatabaseSettings) -> str:
    return settings.url.password or HIDDEN_MARKER


def test_doctor_stdout_is_machine_readable(
    database_kind: DatabaseKind,
    database_settings: DatabaseSettings,
    installation: Installation,
) -> None:
    url = url_text(database_settings.url)

    code, out, err = finish(
        kora_process(["doctor", "--json"], process_environment(installation, url))
    )

    stdout, stderr = stream_texts(out, err)
    assert code == 0, stderr
    report = cast(dict[str, object], json.loads(stdout))
    assert statuses(report) == {name: ("passed", None) for name in CHECKS}
    records = [json.loads(line) for line in stderr.splitlines()]
    assert {"cli.doctor_check_finished", "cli.doctor_finished"} <= {
        record["event"] for record in records
    }
    assert all(ENVIRONMENT_MARKER not in text for text in (stdout, stderr))
    assert all(password_of(database_settings) not in text for text in (stdout, stderr))
    assert str(installation.root) not in stderr


def test_doctor_prints_one_line_per_check(
    database_kind: DatabaseKind,
    database_settings: DatabaseSettings,
    installation: Installation,
) -> None:
    url = url_text(database_settings.url)

    code, out, err = finish(
        kora_process(["doctor"], process_environment(installation, url))
    )

    stdout, stderr = stream_texts(out, err)
    assert code == 0, stderr
    assert stdout.splitlines() == [
        "version passed " + version("kora"),
        f"cel passed {PINNED_VERSION}",
        f"database passed {database_kind.value}",
        f"state_root passed {installation.root}",
    ]
    assert all(json.loads(line) for line in stderr.splitlines())


def group_gone(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    return False


@dataclass(frozen=True)
class ReleaseCase:
    name: str
    interrupt: signal.Signals | None
    exit_code: int
    stdout: list[str]


RELEASE_CASES: list[ReleaseCase] = [
    ReleaseCase("deadline", None, 1, ["database failed deadline"]),
    ReleaseCase("sigint", signal.SIGINT, 130, []),
    ReleaseCase("sigterm", signal.SIGTERM, 130, []),
]


@pytest.mark.parametrize(
    "case", RELEASE_CASES, ids=[case.name for case in RELEASE_CASES]
)
def test_doctor_releases_resources(
    case: ReleaseCase,
    installation: Installation,
    silent_listener: Listener,
) -> None:
    url = postgresql_url(silent_listener.port)
    process = kora_process(["doctor"], process_environment(installation, url))

    if case.interrupt is not None:
        assert silent_listener.accepted.wait(EVENT_DEADLINE_SECONDS)
        process.send_signal(case.interrupt)
    code, out, err = finish(process)

    assert code == case.exit_code, err
    lines = out.decode("utf-8").splitlines()
    assert [line for line in lines if line.startswith("database")] == case.stdout
    assert (lines == []) is (case.interrupt is not None)
    assert group_gone(process)
    assert installation.entries() == []
    assert silent_listener.accepted.is_set()
    assert silent_listener.peer_closed()


def test_doctor_closes_an_unanswered_connection_in_process(
    installation: Installation,
    silent_listener: Listener,
    capfd: pytest.CaptureFixture[str],
) -> None:
    url = postgresql_url(silent_listener.port)

    code, report, _ = run_doctor(
        {"KORA_DATABASE_URL": url}, installation.settings(), capfd
    )

    assert code == 1
    assert statuses(report)["database"] == ("failed", "deadline")
    assert silent_listener.peer_closed()


def open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def test_doctor_in_process_runs_return_to_baseline(
    installation: Installation,
    closing_listener: Listener,
    tmp_path: Path,
    sqlite_template: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    healthy = tmp_path / "kora.db"
    healthy.write_bytes(sqlite_template.read_bytes())
    environments = [
        {"KORA_DATABASE_URL": sqlite_url(healthy)},
        {"KORA_DATABASE_URL": postgresql_url(closing_listener.port)},
    ]
    run_doctor(environments[0], installation.settings(), capfd)
    descriptors = open_descriptors()
    threads = threading.active_count()

    codes = [
        run_doctor(environment, installation.settings(), capfd)[0]
        for _ in range(5)
        for environment in environments
    ]

    assert codes == [0, 1] * 5
    assert open_descriptors() == descriptors
    assert threading.active_count() == threads
    assert installation.entries() == []


async def signal_without_yielding(value: signal.Signals) -> str:
    os.kill(os.getpid(), value)
    return "finished"


@pytest.mark.parametrize(
    "value", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"]
)
def test_signal_during_synchronous_work_interrupts(value: signal.Signals) -> None:
    before = signal.getsignal(value)

    outcome = asyncio.run(run_interruptible(signal_without_yielding(value)))

    assert outcome == ("finished", True)
    assert signal.getsignal(value) == before
