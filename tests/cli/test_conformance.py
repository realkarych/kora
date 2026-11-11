import asyncio
import json
import os
import re
import signal
import socket
import stat
import subprocess
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Literal

import pytest

from kora.cli.conformance import CommandResult
from kora.db.connection import DatabaseSettings
from kora.ir.json import JsonObject
from kora.protocols.lifetime import (
    CleanupOutcome,
    OriginalOutcome,
)
from kora.protocols.metadata import ProtocolName
from tests.support.configured_conformance import (
    installed_conformance as installed_conformance,
)
from tests.support.configured_conformance import vcs_configuration
from tests.support.observability import ProcessResult
from tests.support.provider_tooling import (
    InstalledTooling,
)


def private_home(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def conformance_arguments(
    protocol: str,
    selection: str,
    config: Path,
    fixtures: str,
    data: Path | None = None,
) -> tuple[str, ...]:
    arguments = (
        "conformance",
        protocol,
        selection,
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture." + fixtures,
    )
    return arguments if data is None else (*arguments, "--fixture-data", str(data))


def assert_closed(report: CommandResult) -> None:
    assert report.cleanup is not None
    assert report.cleanup.state is CleanupOutcome.CLOSED


def assert_undisclosed(process: ProcessResult, *markers: bytes) -> None:
    for marker in markers:
        assert marker not in process.stdout + process.stderr


MAX_VCS_PREPARATION_EVIDENCE: Final[int] = 512
VCS_PREPARATION_STEPS: Final[tuple[str, ...]] = (
    "init_remote",
    "init_gone",
    "init_seed",
    "add_seed",
    "commit_seed",
    "push_seed",
    "read_revision",
)
VCS_PREPARATION_ENDINGS: Final[frozenset[bytes]] = frozenset(
    f"git_{step}_{reason}".encode("ascii")
    for step in VCS_PREPARATION_STEPS
    for reason in ("deadline", "nonzero", "signal", "io")
) | {b"prepare_complete"}
VCS_TIMING_RECORD: Final[re.Pattern[bytes]] = re.compile(
    rb"time:("
    + "|".join(VCS_PREPARATION_STEPS).encode("ascii")
    + rb"):(-1|\d{1,9}):\d{1,9}:\d{1,9}"
)


@dataclass(frozen=True)
class VcsPreparationProbe:
    path: Path
    fixture_data: Path
    descriptor: int
    device: int
    inode: int

    def observed(self) -> tuple[str, ...]:
        return self.read()[0]

    def timing(self) -> tuple[str, ...]:
        return self.read()[1]

    def timed_steps(self) -> tuple[str, ...]:
        return tuple(record.split(":")[1] for record in self.timing())

    def read(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            opened = os.fstat(self.descriptor)
            if (
                (opened.st_dev, opened.st_ino) != (self.device, self.inode)
                or not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
            ):
                return ("evidence_unavailable",), ()
            payload = os.pread(self.descriptor, MAX_VCS_PREPARATION_EVIDENCE + 1, 0)
        except OSError:
            return ("evidence_unavailable",), ()
        if not payload:
            return ("entry_not_observed",), ()
        if len(payload) > MAX_VCS_PREPARATION_EVIDENCE or not payload.endswith(b"\n"):
            return ("evidence_malformed",), ()
        records = payload[:-1].split(b"\n")
        timing = [record for record in records if record.startswith(b"time:")]
        closure = [record for record in records if not record.startswith(b"time:")]
        if (
            len(closure) > 2
            or records[0] != b"prepare_entered"
            or (len(closure) == 2 and records[-1] != closure[1])
            or (len(closure) == 2 and closure[1] not in VCS_PREPARATION_ENDINGS)
            or len(timing) > len(VCS_PREPARATION_STEPS)
            or not all(VCS_TIMING_RECORD.fullmatch(record) for record in timing)
        ):
            return ("evidence_malformed",), ()
        return (
            tuple(record.decode("ascii") for record in closure),
            tuple(record.decode("ascii") for record in timing),
        )


@contextmanager
def vcs_preparation_probe(
    tmp_path: Path,
    *,
    occupied_repository: Literal["remote", "gone"] | None = None,
    occupant: Literal["file", "link"] = "file",
    deadline_seconds: int | None = None,
) -> Generator[VcsPreparationProbe]:
    root = private_home(tmp_path / "vcs-observation").resolve(strict=True)
    path = root / "preparation"
    descriptor = os.open(
        path,
        os.O_CREAT
        | os.O_EXCL
        | os.O_RDWR
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | os.O_CLOEXEC,
        0o600,
    )
    try:
        opened = os.fstat(descriptor)
        data = root / "fixture.json"
        data.write_text(
            json.dumps(
                {
                    "vcs_preparation_evidence": {
                        "path": str(path),
                        "device": opened.st_dev,
                        "inode": opened.st_ino,
                        "occupied_repository": occupied_repository,
                        "occupant": occupant,
                        **(
                            {}
                            if deadline_seconds is None
                            else {"deadline_seconds": deadline_seconds}
                        ),
                    }
                }
            ),
            encoding="utf-8",
        )
        yield VcsPreparationProbe(path, data, descriptor, opened.st_dev, opened.st_ino)
    finally:
        os.close(descriptor)


def test_installed_static_token_authentication_conformance(
    installed_conformance: InstalledTooling,
    tmp_path: Path,
    database_settings: DatabaseSettings,
) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}")
    data = tmp_path / "fixture.json"
    data.write_text(
        json.dumps(
            {
                "database_url": database_settings.url.render_as_string(
                    hide_password=False
                )
            }
        )
    )
    home = private_home(tmp_path / "home")

    result = installed_conformance.run(
        *conformance_arguments(
            "authentication", "static_token", config, "fixtures_authentication", data
        ),
        environment={"HOME": str(home)},
    )

    report = CommandResult.model_validate_json(result.stdout)
    assert result.code == 0, (result.stdout, result.stderr)
    assert report.completed == report.expected == 11
    assert report.outcome is OriginalOutcome.COMPLETE
    assert_closed(report)
    assert not list(home.rglob("journal"))
    assert_undisclosed(
        result,
        b"private-installed-authentication-credential",
        b"private-installed-authentication-owner",
        database_settings.url.render_as_string(hide_password=False).encode(),
    )


def test_installed_injected_authentication_suite_needs_no_asyncio_pytest_plugin(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    process = subprocess.run(
        [
            str(installed_conformance.python),
            "-I",
            "-c",
            "import importlib.util, pytest; "
            "assert importlib.util.find_spec('pytest_asyncio') is None; "
            "raise SystemExit(pytest.main(['--pyargs', "
            "'kora_tooling_fixture.fixtures_injected_authentication', "
            "'-q', '-c', '/dev/null', '--noconftest', '-p', 'no:cacheprovider']))",
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert process.returncode == 0, (process.stdout, process.stderr)
    assert b"10 passed" in process.stdout
    assert b"private-" not in process.stdout + process.stderr


@pytest.mark.parametrize("valid", [True, False])
def test_installed_public_conformance_owns_result_and_cleanup(
    installed_conformance: InstalledTooling, tmp_path: Path, valid: bool
) -> None:
    configuration = tmp_path / "private-config.json"
    configuration.write_text(
        json.dumps({"value": "private-public-command-value"}) if valid else "{"
    )
    home = private_home(tmp_path / "home")

    result = installed_conformance.run(
        *conformance_arguments(
            "secrets", "configured", configuration, "fixtures_configured"
        ),
        environment={
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / "state"),
            "KORA_LOG_COMPONENT_LEVELS": "conformance.lifetime=DEBUG",
        },
    )

    assert result.code == (0 if valid else 1), (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["outcome"] == ("complete" if valid else "failed")
    assert report["cleanup"]["state"] == "closed"
    assert report["cleanup"]["original_outcome"] == report["outcome"]
    events = [json.loads(line)["event"] for line in result.stderr.splitlines()]
    assert events.count("conformance.lifetime.started") == 1
    assert events.count("conformance.lifetime.closed") == 1
    if valid:
        assert report["completed"] == report["expected"] == 10
    assert_undisclosed(
        result, b"private-public-command-value", str(configuration).encode()
    )
    assert not list(home.rglob("journal"))


@pytest.mark.parametrize("mode", ["normal", "prepare", "exercise", "teardown"])
def test_conformance_fixture_contract_is_complete(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-command-config-marker"}))
    data.write_text(json.dumps({"mode": mode, "value": "private-exercise-marker"}))
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "secrets", "configured", config, "fixtures_command", data
        ),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert_closed(result)
    assert result.outcome is (
        OriginalOutcome.COMPLETE if mode == "normal" else OriginalOutcome.FAILED
    )
    assert process.code == (0 if mode == "normal" else 1)
    if mode in {"normal", "teardown"}:
        assert result.completed == result.expected == 10
    assert_undisclosed(
        process,
        b"private-command-config-marker",
        b"private-exercise-marker",
        b"private-command-teardown-marker",
        str(config).encode(),
        str(data).encode(),
    )
    assert not list(home.rglob("journal"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "shutdown", "crash"])
async def test_conformance_fixture_resources_are_released(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    installed = installed_conformance
    home = private_home(tmp_path / "home")
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-lifecycle-marker"}))
    with TemporaryDirectory(prefix="kora-feedback-", dir="/tmp") as temporary:
        feedback = str(Path(temporary) / "feedback")
        data.write_text(json.dumps({"mode": mode, "feedback": feedback}))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(feedback)
            listener.listen(1)
            listener.setblocking(False)
            process = await asyncio.create_subprocess_exec(
                *installed.command(
                    *conformance_arguments(
                        "secrets", "configured", config, "fixtures_command", data
                    )
                ),
                cwd=installed.outside,
                env={"HOME": str(home), "PATH": os.defpath, "KORA_LOG_LEVEL": "DEBUG"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(30):
                    loop = asyncio.get_running_loop()
                    endpoint, _ = await loop.sock_accept(listener)
                    with endpoint:
                        invocation = await loop.sock_recv(endpoint, 32)
                        assert len(invocation) == 32

                        if mode == "shutdown":
                            await loop.sock_sendall(endpoint, b"a")
                        else:
                            process.send_signal(
                                signal.SIGKILL if mode == "crash" else signal.SIGTERM
                            )
                        if mode == "cancel":
                            assert process.stderr is not None
                            while (
                                b'"event":"conformance.lifetime.cancelled"'
                                not in await process.stderr.readline()
                            ):
                                assert process.returncode is None
                            process.send_signal(signal.SIGINT)
                        output, errors = await process.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
                await process.communicate()

    if mode == "crash":
        assert process.returncode == -signal.SIGKILL
        recovered = installed.run(
            "conformance",
            "cleanup",
            invocation.decode("ascii"),
            environment={"HOME": str(home)},
        )
        assert recovered.code == 0, (recovered.stdout, recovered.stderr)
        result = CommandResult.model_validate_json(recovered.stdout)
        assert result.reason == "cleanup_complete"
        output += recovered.stdout
        errors += recovered.stderr
    else:
        result = CommandResult.model_validate_json(output)
        assert process.returncode == (130 if mode == "cancel" else 1)
        assert result.outcome is (
            OriginalOutcome.CANCELLED if mode == "cancel" else OriginalOutcome.FAILED
        )
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert result.cleanup.unresolved_count == 0
    assert not list(home.rglob("journal"))
    assert b"private-lifecycle-marker" not in output + errors
    assert feedback.encode() not in output + errors


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pre_unlink", "post_unlink", "substituted"])
async def test_cleanup_command_preserves_unverified_socket_recovery(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    home = private_home(tmp_path / "home")
    installed = installed_conformance
    process = await asyncio.create_subprocess_exec(
        str(installed.python),
        "-I",
        str(Path(__file__).parents[1] / "conformance" / "command_retention_probe.py"),
        str(home),
        mode,
        env={"PATH": os.defpath},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            assert process.stdout is not None
            invocation = (await process.stdout.readexactly(32)).decode("ascii")

            process.kill()
            await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
    assert process.returncode == -signal.SIGKILL
    for _ in range(1 if mode == "post_unlink" else 2):
        completed = installed.run(
            "conformance",
            "cleanup",
            invocation,
            environment={"HOME": str(home), "XDG_STATE_HOME": str(home / "state")},
        )
        report = CommandResult.model_validate_json(completed.stdout)
        assert report.cleanup is not None
        assert completed.code == (0 if mode == "post_unlink" else 1)
        if mode == "post_unlink":
            assert report.cleanup.state is CleanupOutcome.CLOSED
            assert not list(home.rglob("journal"))
        else:
            assert report.cleanup.state is CleanupOutcome.UNKNOWN
            retained = next(home.rglob(invocation))
            assert (retained / "journal").is_file()
            assert (retained / "claim").is_file()
            if mode == "substituted":
                assert report.cleanup.original_outcome is OriginalOutcome.FAILED


@pytest.mark.parametrize("selection", ["configured", "fake", "missing"])
def test_targeted_conformance_loads_only_selected_provider(
    installed_conformance: InstalledTooling, tmp_path: Path, selection: str
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"value": "private-selection-value"}))
    home = private_home(tmp_path / "home")
    environment = {
        "HOME": str(home),
        "KORA_LOG_LEVEL": "DEBUG",
        "PYTHONPATH": str(tmp_path),
        "PYTHONHOME": str(tmp_path),
        "PYTEST_PLUGINS": "unrelated.private_plugin",
        "PYTEST_ADDOPTS": "--private-ambient-option",
        "KORA_TOKEN": "private-environment-value",
    }

    process = installed_conformance.run(
        *conformance_arguments("secrets", selection, config, "fixtures_configured"),
        environment=environment,
    )

    report = CommandResult.model_validate_json(process.stdout)
    assert process.code == (0 if selection == "configured" else 1)
    assert_closed(report)
    records = process.records()
    completed = [
        record for record in records if record["event"] == "cli.conformance_finished"
    ]
    assert len(completed) == 1
    assert completed[0]["outcome"] == report.outcome.value
    assert completed[0]["expected"] == report.expected
    assert_undisclosed(
        process,
        b"private-selection-value",
        b"unrelated.private_plugin",
        b"private-ambient-option",
        b"private-environment-value",
    )


@pytest.mark.parametrize(
    "plugin",
    ["fixtures_missing", "fixtures_wrong_bundle", "fixtures_replaced", "not_installed"],
)
def test_conformance_inputs_are_not_disclosed(
    installed_conformance: InstalledTooling, tmp_path: Path, plugin: str
) -> None:
    config = tmp_path / "private-config-marker.json"
    data = tmp_path / "private-data-marker.json"
    config.write_text(json.dumps({"value": "private-rejected-config-marker"}))
    data.write_text(json.dumps({"value": "private-rejected-exercise-marker"}))
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments("secrets", "configured", config, plugin, data),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert result.outcome is OriginalOutcome.FAILED
    assert_closed(result)
    assert result.completed == 0
    assert_undisclosed(
        process,
        config.name.encode(),
        data.name.encode(),
        b"private-rejected-config-marker",
        b"private-rejected-exercise-marker",
    )


def test_installed_provider_schema_matches_metadata(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-schema-marker"}))
    data.write_text(json.dumps({"mode": "schema"}))
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "secrets", "configured", config, "fixtures_command", data
        ),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert result.outcome is OriginalOutcome.FAILED
    assert result.completed == 0
    assert_closed(result)
    assert_undisclosed(process, b"private-schema-marker")


@pytest.mark.parametrize(
    ("protocol", "plugin", "configuration", "expected"),
    [
        (ProtocolName.SECRETS, "fixtures_configured", {"value": "secret-value"}, 10),
        (ProtocolName.POLICY, "fixtures_policy", {"deny": ["Bash(git *)"]}, 13),
        (ProtocolName.RUNTIME, "fixtures_runtime", {"value": "configured-domain"}, 5),
        (ProtocolName.HARNESS, "fixtures_harness", {"prefix": "configured-"}, 10),
        (ProtocolName.VCS, "fixtures_vcs", None, 13),
        (
            ProtocolName.ARTIFACT_STORAGE,
            "fixtures_storage",
            {"root": "store", "limits": {"version": 1}},
            18,
        ),
    ],
)
def test_installed_provider_supports_local_tooling(
    installed_conformance: InstalledTooling,
    tmp_path: Path,
    protocol: ProtocolName,
    plugin: str,
    configuration: JsonObject | None,
    expected: int,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    installed = installed_conformance
    home = private_home(installed.outside / protocol.value)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            configuration
            if configuration is not None
            else vcs_configuration(Path("vcs"))
        )
    )

    with ExitStack() as resources:
        probe = (
            resources.enter_context(vcs_preparation_probe(tmp_path))
            if protocol is ProtocolName.VCS
            else None
        )
        process = installed.run(
            *conformance_arguments(
                protocol.value,
                "configured",
                config,
                plugin,
                probe.fixture_data if probe is not None else None,
            ),
            environment={
                "HOME": str(home),
                "KORA_LOG_COMPONENT_LEVELS": "protocols.loading=DEBUG",
            },
        )
        preparation = probe.observed() if probe is not None else ()
        timed_steps = probe.timed_steps() if probe is not None else ()
        if probe is not None:
            record_testsuite_property(
                "vcs_preparation_timing_first", ";".join(probe.timing())
            )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr, preparation)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected == expected
    assert_closed(outcome)
    records = process.records()
    assert len(records) == 1
    assert records[0]["event"] == "protocols.loading.provider_loaded"
    assert records[0]["protocol"] == protocol.value
    assert not list(home.rglob("journal"))
    if protocol is ProtocolName.VCS:
        assert preparation == ("prepare_entered", "prepare_complete")
        assert timed_steps == VCS_PREPARATION_STEPS


@pytest.mark.parametrize(
    ("occupied_repository", "expected_steps"),
    [("remote", ("init_remote",)), ("gone", ("init_remote", "init_gone"))],
)
def test_installed_vcs_reports_git_preparation_refusal_without_exposing_inputs(
    installed_conformance: InstalledTooling,
    tmp_path: Path,
    record_testsuite_property: Callable[[str, object], None],
    occupied_repository: Literal["remote", "gone"],
    expected_steps: tuple[str, ...],
) -> None:
    home = private_home(tmp_path / "home")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(vcs_configuration(Path("vcs-private-setup-canary"))))

    with vcs_preparation_probe(
        tmp_path, occupied_repository=occupied_repository
    ) as probe:
        process = installed_conformance.run(
            *conformance_arguments(
                "vcs", "configured", config, "fixtures_vcs", probe.fixture_data
            ),
            environment={"HOME": str(home)},
            umask=0o022,
        )
        preparation = probe.observed()
        timing = probe.timing()
        timed_steps = probe.timed_steps()
    if occupied_repository == "remote":
        record_testsuite_property("vcs_preparation_timing_later", ";".join(timing))
    exposed = any(
        marker in process.stdout + process.stderr
        for marker in (
            b"vcs-private-setup-canary",
            b"remote.git",
            b"gone.git",
            b"git-trace",
        )
    )

    assert not exposed
    assert preparation == ("prepare_entered", f"git_{expected_steps[-1]}_nonzero")
    assert timed_steps == expected_steps
    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert outcome.outcome is OriginalOutcome.FAILED
    assert outcome.reason == "refused"
    assert outcome.completed == 0
    assert outcome.expected == 13
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    assert outcome.cleanup.unresolved_count == 0
    assert not list(home.rglob("journal"))


def test_installed_vcs_disposes_partial_preparation_with_git_link(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    home = private_home(tmp_path / "home")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(vcs_configuration(Path("vcs-private-link-canary"))))

    with vcs_preparation_probe(
        tmp_path, occupied_repository="remote", occupant="link"
    ) as probe:
        process = installed_conformance.run(
            *conformance_arguments(
                "vcs", "configured", config, "fixtures_vcs", probe.fixture_data
            ),
            environment={"HOME": str(home)},
        )
        preparation = probe.observed()
        timed_steps = probe.timed_steps()

    assert_undisclosed(process, b"vcs-private-link-canary", b"remote.git", b"git-trace")
    assert preparation == ("prepare_entered", "git_init_remote_nonzero")
    assert timed_steps == ("init_remote",)
    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert outcome.outcome is OriginalOutcome.FAILED
    assert outcome.reason == "refused"
    assert outcome.completed == 0
    assert outcome.expected == 13
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    assert outcome.cleanup.unresolved_count == 0
    assert not list(home.rglob("journal"))


def test_installed_vcs_reports_git_deadline_with_timing(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    home = private_home(tmp_path / "home")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(vcs_configuration(Path("vcs"))))

    with vcs_preparation_probe(tmp_path, deadline_seconds=0) as probe:
        process = installed_conformance.run(
            *conformance_arguments(
                "vcs", "configured", config, "fixtures_vcs", probe.fixture_data
            ),
            environment={"HOME": str(home)},
        )
        preparation = probe.observed()
        timed_steps = probe.timed_steps()

    assert_undisclosed(process, b"remote.git", b"git-trace")
    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert outcome.outcome is OriginalOutcome.FAILED
    assert outcome.reason == "refused"
    assert outcome.completed == 0
    assert outcome.expected == 13
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    assert outcome.cleanup.unresolved_count == 0
    assert preparation == ("prepare_entered", "git_init_remote_deadline")
    assert timed_steps == ("init_remote",)
    assert not list(home.rglob("journal"))


def test_installed_vcs_refuses_to_seed_without_configured_git(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    home = private_home(tmp_path / "home")
    tool_directory = tmp_path / "private-git-path-canary"
    tool_directory.mkdir(mode=0o700)
    configuration = vcs_configuration(Path("vcs"))
    configuration["tool_path"] = [str(tool_directory)]
    config = tmp_path / "config.json"
    config.write_text(json.dumps(configuration))

    with vcs_preparation_probe(tmp_path) as probe:
        process = installed_conformance.run(
            *conformance_arguments(
                "vcs", "configured", config, "fixtures_vcs", probe.fixture_data
            ),
            environment={"HOME": str(home)},
        )
        preparation = probe.observed()
        timing = probe.timing()

    assert_undisclosed(process, b"private-git-path-canary", b"remote.git", b"git-trace")
    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert outcome.outcome is OriginalOutcome.FAILED
    assert outcome.reason == "refused"
    assert outcome.completed == 0
    assert outcome.expected == 13
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    assert outcome.cleanup.unresolved_count == 0
    assert preparation == ("prepare_entered",)
    assert not timing
    assert not list(home.rglob("journal"))


def test_installed_vcs_ignores_replaced_preparation_evidence_file(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    home = private_home(tmp_path / "home")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(vcs_configuration(Path("vcs"))))
    with vcs_preparation_probe(tmp_path) as probe:
        original = probe.path.rename(probe.path.with_name("original"))
        probe.path.touch(mode=0o600, exist_ok=False)

        process = installed_conformance.run(
            *conformance_arguments(
                "vcs", "configured", config, "fixtures_vcs", probe.fixture_data
            ),
            environment={"HOME": str(home)},
        )
        preparation = probe.observed()
        original_size = original.stat().st_size
        replacement_size = probe.path.stat().st_size

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected == 13
    assert_closed(outcome)
    assert preparation == ("entry_not_observed",)
    assert original_size == replacement_size == 0
    assert not list(home.rglob("journal"))


@pytest.mark.parametrize("value", ["first-effective-value", "second-effective-value"])
def test_installed_provider_configuration_is_effective(
    installed_conformance: InstalledTooling,
    tmp_path: Path,
    value: str,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"value": value}))
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments("secrets", "configured", config, "fixtures_configured"),
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert_closed(outcome)
    assert_undisclosed(process, value.encode())


def test_installed_conformance_owns_its_environment_and_wheel_copy(
    installed_conformance: InstalledTooling, core_wheel: Path
) -> None:
    root = installed_conformance.outside.parent
    copy = root / "dependencies" / core_wheel.name

    copied, shared = copy.lstat(), core_wheel.stat()

    assert stat.S_ISREG(copied.st_mode)
    assert (copied.st_dev, copied.st_ino) != (shared.st_dev, shared.st_ino)
    assert copy.read_bytes() == core_wheel.read_bytes()
    assert installed_conformance.python.is_relative_to(root)
    assert not core_wheel.is_relative_to(root)


def test_installed_vcs_ref_artifact_type_conformance(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}")
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "artifact_type", "vcs_ref", config, "fixtures_vcs_ref_artifact_type"
        ),
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected
    assert outcome.expected > 0
    assert_closed(outcome)
    assert not list(home.rglob("journal"))
    assert_undisclosed(
        process,
        b"private-repository-marker",
        b"private-branch-marker",
        b"private-path-marker",
        b"private-diff-marker",
        b"private-principal-marker",
    )


def test_installed_agent_session_artifact_type_conformance(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}")
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "artifact_type",
            "agent_session",
            config,
            "fixtures_agent_session_artifact_type",
        ),
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected
    assert outcome.expected > 0
    assert_closed(outcome)
    assert not list(home.rglob("journal"))
    assert_undisclosed(
        process,
        b"private-agent-session-price-marker",
        b"private-agent-session-usage-marker",
        b"private-agent-session-coverage-marker",
        b"private-agent-session-denial-marker",
    )


def test_installed_structured_output_artifact_type_conformance(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}")
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "artifact_type",
            "structured_output",
            config,
            "fixtures_structured_output_artifact_type",
        ),
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected == 10
    assert_closed(outcome)
    assert not list(home.rglob("journal"))
    assert_undisclosed(
        process,
        b"private-structured-output-payload-marker",
        b"private-structured-output-key-marker",
        b"private-structured-output-invalid-marker",
    )


def test_installed_command_result_artifact_type_conformance(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}")
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "artifact_type",
            "command_result",
            config,
            "fixtures_command_result_artifact_type",
        ),
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected
    assert outcome.expected > 0
    assert_closed(outcome)
    assert not list(home.rglob("journal"))
    assert_undisclosed(
        process,
        b"private-command-result-marker",
        b"cHJpdmF0ZS1jb21tYW5kLXJlc3VsdC1tYXJrZXI=",
    )


@pytest.mark.parametrize("selected", ["left", "right"])
def test_installed_artifact_type_configuration_is_effective(
    installed_conformance: InstalledTooling, tmp_path: Path, selected: str
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"selected": selected}))
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "artifact_type", "configured", config, "fixtures_artifact_type"
        ),
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected
    assert_closed(outcome)
    assert not list(home.rglob("journal"))
    assert_undisclosed(process, b"private-left-marker", b"private-right-marker")


def test_installed_artifact_type_rejects_ignored_configuration(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"selected": "right"}))
    data.write_text(json.dumps({"ignore_configuration": True}))
    home = private_home(tmp_path / "home")

    process = installed_conformance.run(
        *conformance_arguments(
            "artifact_type", "configured", config, "fixtures_artifact_type", data
        ),
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.FAILED
    assert_closed(outcome)
    assert not list(home.rglob("journal"))
    assert_undisclosed(process, b"private-left-marker", b"private-right-marker")
