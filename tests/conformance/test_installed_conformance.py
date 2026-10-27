import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Generator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.cli.conformance import CommandResult
from kora.conformance.controllers import duration_ns
from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.conformance.result import ChildInput, ChildResult, InputFiles
from kora.conformance.transport import LifetimeEndpoint
from kora.ir.json import JsonObject
from kora.protocols.lifetime import (
    CleanupOutcome,
    ClosureReport,
    EnvironmentVariable,
    OriginalOutcome,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.metadata import ProtocolName
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel, ChannelError
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.support.provider_tooling import (
    InstalledTooling,
    build_core_wheel,
    build_fixture_wheel,
    install_tooling,
    prepare_wheelhouse,
)


def vcs_configuration(root: Path) -> JsonObject:
    principal: JsonObject = {
        "provider_instance": "fixture-git",
        "stable_id": "fixture-publisher",
        "display_name": "Fixture publisher",
    }
    return {
        "remotes": {
            "local": {
                "path": str(root / "remote.git"),
                "binding": "fixture-binding",
                "binding_revision": 1,
                "credential_mode": "local_credential_free",
                "principal": principal,
            },
            "gone": {
                "path": str(root / "gone.git"),
                "binding": "gone-binding",
                "binding_revision": 1,
                "credential_mode": "local_credential_free",
                "principal": principal,
            },
        },
        "identity": {"name": "Fixture", "email": "fixture@example.invalid"},
        "tool_path": list(os.environ["PATH"].split(":")),
    }


@pytest.fixture(scope="module")
def installed_conformance() -> Generator[InstalledTooling]:
    with TemporaryDirectory(prefix="kora-installed-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        wheelhouse = root / "dependencies"
        wheelhouse.mkdir()
        prepare_wheelhouse(wheelhouse, conformance=True)
        build_core_wheel(wheelhouse)
        fixture = build_fixture_wheel(
            root,
            {
                "secrets.configured": (
                    "kora_tooling_fixture.configured_secrets:configured"
                ),
                "secrets.blocked": "kora_tooling_fixture.blocked_metadata:blocked",
                "policy.unrelated": (
                    "kora_tooling_fixture.noisy_import:provider_metadata"
                ),
                "policy.configured": (
                    "kora_tooling_fixture.configured_policy:configured"
                ),
                "runtime.configured": (
                    "kora_tooling_fixture.configured_runtime:configured"
                ),
                "harness.configured": (
                    "kora_tooling_fixture.configured_harness:configured"
                ),
                "vcs.configured": "kora_tooling_fixture.configured_vcs:configured",
                "artifact_storage.configured": (
                    "kora_tooling_fixture.configured_storage:configured"
                ),
            },
            {"socket": str(root)},
        )
        yield install_tooling(root, wheelhouse, fixture, conformance=True)


@pytest.mark.parametrize("valid", [True, False])
def test_installed_public_conformance_owns_result_and_cleanup(
    installed_conformance: InstalledTooling, tmp_path: Path, valid: bool
) -> None:
    configuration = tmp_path / "private-config.json"
    configuration.write_text(
        json.dumps({"value": "private-public-command-value"}) if valid else "{"
    )
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    result = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(configuration),
        "--fixtures",
        "kora_tooling_fixture.fixtures_configured",
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
    assert b"private-public-command-value" not in result.stdout + result.stderr
    assert str(configuration).encode() not in result.stdout + result.stderr
    assert not list(home.rglob("journal"))


@pytest.mark.parametrize("mode", ["normal", "prepare", "exercise", "teardown"])
def test_conformance_fixture_contract_is_complete(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-command-config-marker"}))
    data.write_text(json.dumps({"mode": mode, "value": "private-exercise-marker"}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_command",
        "--fixture-data",
        str(data),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert result.outcome is (
        OriginalOutcome.COMPLETE if mode == "normal" else OriginalOutcome.FAILED
    )
    assert process.code == (0 if mode == "normal" else 1)
    if mode in {"normal", "teardown"}:
        assert result.completed == result.expected == 10
    for marker in (
        b"private-command-config-marker",
        b"private-exercise-marker",
        b"private-command-teardown-marker",
        str(config).encode(),
        str(data).encode(),
    ):
        assert marker not in process.stdout + process.stderr
    assert not list(home.rglob("journal"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "shutdown", "crash"])
async def test_conformance_fixture_resources_are_released(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    installed = installed_conformance
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
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
                str(installed.python),
                "-I",
                "-m",
                "kora",
                "conformance",
                "secrets",
                "configured",
                "--config",
                str(config),
                "--fixtures",
                "kora_tooling_fixture.fixtures_command",
                "--fixture-data",
                str(data),
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
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    installed = installed_conformance
    process = await asyncio.create_subprocess_exec(
        str(installed.python),
        "-I",
        str(Path(__file__).with_name("command_retention_probe.py")),
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


@pytest.mark.parametrize("mode", ["idle", "delayed_teardown", "bootstrap"])
def test_command_waits_for_ready_execution_and_late_teardown(
    installed_conformance: InstalledTooling, tmp_path: Path, mode: str
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = subprocess.run(
        [
            str(installed_conformance.python),
            "-I",
            str(Path(__file__).with_name("command_barrier_probe.py")),
            str(home),
            mode,
            str(installed_conformance.outside.parent),
        ],
        env={"PATH": os.defpath},
        cwd=installed_conformance.outside,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert process.returncode == 0, process.stderr
    report = CommandResult.model_validate_json(process.stdout)
    assert report.outcome is (
        OriginalOutcome.COMPLETE if mode == "idle" else OriginalOutcome.FAILED
    )
    assert report.cleanup is not None
    assert report.cleanup.state is CleanupOutcome.CLOSED
    assert not list(home.rglob("journal"))
    assert b"private-barrier-value" not in process.stdout + process.stderr


@pytest.mark.parametrize("selection", ["configured", "fake", "missing"])
def test_targeted_conformance_loads_only_selected_provider(
    installed_conformance: InstalledTooling, tmp_path: Path, selection: str
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"value": "private-selection-value"}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
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
        "conformance",
        "secrets",
        selection,
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_configured",
        environment=environment,
    )

    report = CommandResult.model_validate_json(process.stdout)
    assert process.code == (0 if selection == "configured" else 1)
    assert report.cleanup is not None
    assert report.cleanup.state is CleanupOutcome.CLOSED
    records = process.records()
    completed = [
        record for record in records if record["event"] == "cli.conformance_finished"
    ]
    assert len(completed) == 1
    assert completed[0]["outcome"] == report.outcome.value
    assert completed[0]["expected"] == report.expected
    for marker in (
        b"private-selection-value",
        b"unrelated.private_plugin",
        b"private-ambient-option",
        b"private-environment-value",
    ):
        assert marker not in process.stdout + process.stderr


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
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture." + plugin,
        "--fixture-data",
        str(data),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert result.outcome is OriginalOutcome.FAILED
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert result.completed == 0
    for marker in (
        config.name,
        data.name,
        "private-rejected-config-marker",
        "private-rejected-exercise-marker",
    ):
        assert marker.encode() not in process.stdout + process.stderr


def test_installed_provider_schema_matches_metadata(
    installed_conformance: InstalledTooling, tmp_path: Path
) -> None:
    config = tmp_path / "config.json"
    data = tmp_path / "data.json"
    config.write_text(json.dumps({"value": "private-schema-marker"}))
    data.write_text(json.dumps({"mode": "schema"}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_command",
        "--fixture-data",
        str(data),
        environment={"HOME": str(home)},
    )

    result = CommandResult.model_validate_json(process.stdout)
    assert process.code == 1
    assert result.outcome is OriginalOutcome.FAILED
    assert result.completed == 0
    assert result.cleanup is not None
    assert result.cleanup.state is CleanupOutcome.CLOSED
    assert b"private-schema-marker" not in process.stdout + process.stderr


@dataclass(frozen=True)
class Outcome:
    result: ChildResult
    closure: ClosureReport
    events: bytes
    raw_bytes: int
    returncode: int | None
    phase_bytes: tuple[int, ...]


async def drained(endpoint: socket.socket) -> bytes:
    data = bytearray()
    try:
        while chunk := await asyncio.get_running_loop().sock_recv(endpoint, 16384):
            data.extend(chunk)
            assert len(data) <= 1048576
        return bytes(data)
    finally:
        endpoint.close()


async def installed_run(
    installed: InstalledTooling,
    mode: str = "normal",
    *,
    configuration: JsonObject | None = None,
    plugin: str = "fixtures_configured",
    protocol: ProtocolName = ProtocolName.SECRETS,
    feedback_phases: bytes = b"",
    stop_on_feedback: bool = False,
    entrypoint: str = "kora.conformance.child",
    files: InputFiles | None = None,
) -> Outcome:
    with TemporaryDirectory(prefix="kora-invoke-", dir="/tmp") as temporary:
        base = Path(temporary).resolve()
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            clock=clock,
            launcher=InstalledIOHelperLauncher(home=base, state_home=base / "state"),
        )
        tasks: list[asyncio.Task[object]] = []
        try:
            async with AsyncExitStack() as resources:
                preparation = await resources.enter_async_context(
                    asyncio.timeout(owner.limits.acquisition_seconds)
                )
                await owner.ready()
                listeners: list[socket.socket] = []
                for name in ("result", "events", "lifetime", "feedback"):
                    listener = resources.enter_context(
                        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    )
                    listener.bind(str(base / name))
                    listener.listen(1)
                    listener.setblocking(False)
                    listeners.append(listener)
                tree = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                acquired_tree = await owner.acquire(
                    tree.token, new_operation_id(), TemporaryTreeRequest()
                )
                await owner.acknowledge(
                    tree.token, acquired_tree.operation_id, acquired_tree.revision
                )
                startup = owner.records[tree.token.resource_id].directory
                assert startup is not None
                for name, payload in (
                    ("conftest.py", b'raise RuntimeError("ambient-conftest-loaded")\n'),
                    (
                        "pytest.ini",
                        b"[pytest]\naddopts = --ambient-option-must-not-load\n",
                    ),
                ):
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=startup.descriptor,
                    )
                    try:
                        assert os.write(descriptor, payload) == len(payload)
                    finally:
                        os.close(descriptor)
                child = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.PROCESS_DOMAIN,
                        parent_resource_id=tree.token.resource_id,
                    )
                )
                environment = {
                    "HOME": str(base),
                    "PATH": os.environ["PATH"],
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "KORA_FIXTURE_MODE": mode,
                    "KORA_FIXTURE_FEEDBACK": str(base / "feedback"),
                }
                acquired = await owner.acquire(
                    child.token,
                    new_operation_id(),
                    ProcessLaunch(
                        executable=str(installed.python),
                        argv=(
                            str(installed.python),
                            "-I",
                            "-W",
                            "error",
                            "-m",
                            entrypoint,
                            *(
                                str(base / name)
                                for name in ("result", "events", "lifetime")
                            ),
                        ),
                        environment=tuple(
                            EnvironmentVariable(name=k, value=v)
                            for k, v in environment.items()
                        ),
                        working_directory_resource_id=tree.token.resource_id,
                        stdin=None,
                        stdout=ProcessOutput.CAPTURE,
                        stderr=ProcessOutput.CAPTURE,
                        domain=ProcessDomain.INVOCATION,
                    ),
                )
                await owner.acknowledge(
                    child.token, acquired.operation_id, acquired.revision
                )
                preparation.reschedule(None)
                process = owner.records[child.token.resource_id].process
                assert process is not None
                loop = asyncio.get_running_loop()

                async def lifetime() -> ClosureReport:
                    endpoint, _ = await loop.sock_accept(listeners[2])
                    return await LifetimeEndpoint(
                        owner, child.token.resource_id, endpoint
                    ).run()

                async def events() -> bytes:
                    endpoint, _ = await loop.sock_accept(listeners[1])
                    return await drained(endpoint)

                async def feedback() -> tuple[int, ...]:
                    observed: list[int] = []
                    for phase in feedback_phases:
                        endpoint, _ = await loop.sock_accept(listeners[3])
                        try:
                            assert await loop.sock_recv(endpoint, 1) == bytes([phase])
                            observed.append(process.capture_bytes)
                            await loop.sock_sendall(endpoint, b"a")
                            if stop_on_feedback:
                                invocation = owner.mutations.directory
                                assert invocation is not None
                                live_cases = [
                                    record
                                    for key, record in owner.records.items()
                                    if key != tree.token.resource_id
                                    and record.directory is not None
                                    and not record.directory.closed
                                ]
                                assert len(live_cases) == 1
                                case = live_cases[0]
                                assert case.entry.resource_id in os.listdir(
                                    invocation.descriptor
                                )
                                assert process.observe().live_members
                                owner.request_close(None)
                                clock.advance(
                                    duration_ns(owner.limits.cooperative_seconds) + 1
                                )
                                closure = await owner.close(None)
                                assert closure.state is CleanupOutcome.CLOSED
                                assert process.closed
                                assert process.returncode is not None
                                assert invocation.closed
                                assert owner.inventory().retained_trees == 0
                        finally:
                            endpoint.close()
                    return tuple(observed)

                serving = asyncio.create_task(lifetime())
                reading = asyncio.create_task(events())
                observing = asyncio.create_task(feedback())
                tasks.extend((serving, reading, observing))
                endpoint, _ = await asyncio.wait_for(loop.sock_accept(listeners[0]), 30)
                channel = BoundedChannel(endpoint, SystemClock())
                try:
                    deadline = SystemClock().monotonic_ns() + 30_000_000_000
                    if protocol is ProtocolName.VCS and configuration is None:
                        state = (
                            base / "Library" / "Application Support"
                            if sys.platform == "darwin"
                            else base / "state"
                        )
                        root = (
                            state
                            / "kora"
                            / "conformance"
                            / owner.invocation_id
                            / tree.token.resource_id
                            / "vcs"
                        )
                        configuration = vcs_configuration(root)
                    request = ChildInput(
                        protocol=protocol,
                        name="configured",
                        fixtures="kora_tooling_fixture." + plugin,
                        files=files,
                    )
                    await channel.write_record(
                        request.model_dump_json().encode(), deadline
                    )
                    await channel.write_record(
                        json.dumps(
                            {"value": "private-secret-value"}
                            if configuration is None
                            else configuration
                        ).encode(),
                        deadline,
                    )
                    await channel.write_record(
                        b'{"value":"conflicting-fixture-value"}', deadline
                    )
                    if stop_on_feedback:
                        await asyncio.wait_for(observing, 20)
                    result = ChildResult.model_validate_json(
                        await channel.read_record(deadline, 4096)
                    )
                    if result.phase == "ready":
                        assert result.expected > 0
                        result = ChildResult.model_validate_json(
                            await channel.read_record(deadline, 4096)
                        )
                    if result.phase == "storage":
                        invocation = owner.mutations.directory
                        assert invocation is not None
                        retained = [
                            record
                            for key, record in owner.records.items()
                            if key != tree.token.resource_id
                            and record.directory is not None
                            and not record.directory.closed
                        ]
                        assert len(retained) == 1
                        case = retained[0]
                        assert case.entry.resource_id in os.listdir(
                            invocation.descriptor
                        )
                        assert process.observe().live_members
                        if mode == "preexisting":
                            directory = case.directory
                            assert directory is not None
                            descriptor = os.open(
                                "store/borrowed",
                                os.O_RDONLY | os.O_NOFOLLOW,
                                dir_fd=directory.descriptor,
                            )
                            try:
                                assert os.read(descriptor, 128) == (
                                    b"preserve-before-parent-stop"
                                )
                            finally:
                                os.close(descriptor)
                        owner.request_close(None)
                        clock.advance(duration_ns(owner.limits.cooperative_seconds) + 1)
                finally:
                    channel.close()
                event_bytes = await asyncio.wait_for(reading, 5)
                phase_bytes = await asyncio.wait_for(observing, 5)
                closure = await asyncio.wait_for(owner.close(None), 20)
                raw_bytes = process.capture_bytes
                assert process.closed
                assert owner.inventory().retained_processes == 0
                assert not owner.mutations.helpers
                return Outcome(
                    result,
                    closure,
                    event_bytes,
                    raw_bytes,
                    process.returncode,
                    phase_bytes,
                )
        finally:
            try:
                await dispose_test_owner(owner, clock)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failures = [
                    result
                    for result in results
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                ]
                if failures:
                    raise BaseExceptionGroup("installed harness tasks failed", failures)


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
) -> None:
    installed = installed_conformance
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            configuration
            if configuration is not None
            else vcs_configuration(Path("vcs"))
        )
    )

    process = installed.run(
        "conformance",
        protocol.value,
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture." + plugin,
        environment={
            "HOME": str(home),
            "KORA_LOG_COMPONENT_LEVELS": "protocols.loading=DEBUG",
        },
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.completed == outcome.expected == expected
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    records = process.records()
    assert len(records) == 1
    assert records[0]["event"] == "protocols.loading.provider_loaded"
    assert records[0]["protocol"] == protocol.value
    assert not list(home.rglob("journal"))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["setup", "exercise", "teardown"])
@pytest.mark.parametrize("kind", ["skip", "xfail"])
async def test_installed_conformance_requires_fixture_teardown(
    installed_conformance: InstalledTooling,
    phase: str,
    kind: str,
) -> None:
    mode = f"{phase}_{kind}"

    outcome = await installed_run(
        installed_conformance, mode, plugin="fixtures_adverse"
    )

    assert not outcome.result.accepted
    assert outcome.returncode != 0
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert not outcome.result.report_accepted
    if phase == "teardown":
        assert outcome.result.completed == outcome.result.expected == 10
        assert outcome.result.pytest_status == 0
        assert outcome.result.wrapper_closed
    assert b"private-secret-value" not in outcome.events
    assert "private-secret-value" not in outcome.result.model_dump_json()


@pytest.mark.asyncio
async def test_installed_conformance_owns_storage_cases(
    installed_conformance: InstalledTooling,
) -> None:
    configuration: JsonObject = {"root": "other-store", "limits": {"version": 1}}

    outcome = await installed_run(
        installed_conformance,
        configuration=configuration,
        protocol=ProtocolName.ARTIFACT_STORAGE,
        plugin="fixtures_storage",
    )

    assert outcome.result.accepted, outcome
    assert outcome.result.completed == 18
    assert outcome.closure.state is CleanupOutcome.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver", ["destinations", "sources", "heads"])
async def test_installed_vcs_rejects_alternate_executor_receivers(
    installed_conformance: InstalledTooling,
    receiver: str,
) -> None:
    mode = "decoy_" + receiver

    outcome = await installed_run(
        installed_conformance, mode, protocol=ProtocolName.VCS, plugin="fixtures_vcs"
    )

    assert not outcome.result.accepted
    assert outcome.result.completed == 5
    assert outcome.returncode != 0
    assert outcome.closure.state is CleanupOutcome.CLOSED


@pytest.mark.parametrize("value", ["first-effective-value", "second-effective-value"])
def test_installed_provider_configuration_is_effective(
    installed_conformance: InstalledTooling,
    tmp_path: Path,
    value: str,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"value": value}))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    process = installed_conformance.run(
        "conformance",
        "secrets",
        "configured",
        "--config",
        str(config),
        "--fixtures",
        "kora_tooling_fixture.fixtures_configured",
        environment={"HOME": str(home)},
    )

    outcome = CommandResult.model_validate_json(process.stdout)
    assert process.code == 0, (process.stdout, process.stderr)
    assert outcome.outcome is OriginalOutcome.COMPLETE
    assert outcome.cleanup is not None
    assert outcome.cleanup.state is CleanupOutcome.CLOSED
    assert value.encode() not in process.stdout + process.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "setup",
        "prepare",
        "bind",
        "construct",
        "schema",
        "exercise",
        "cleanup",
        "teardown",
        "echo",
    ],
)
async def test_installed_conformance_preserves_ordinary_failure_controls(
    installed_conformance: InstalledTooling,
    mode: str,
) -> None:
    installed = installed_conformance

    outcome = await installed_run(installed, mode)

    assert not outcome.result.accepted
    assert outcome.returncode != 0
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert "private-" not in outcome.result.model_dump_json()
    if mode == "teardown":
        assert outcome.result.completed == 10
        assert outcome.result.pytest_status == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["generic", "selected", "adapter"])
async def test_installed_conformance_rejects_fixture_replacement(
    installed_conformance: InstalledTooling,
    mode: str,
) -> None:
    installed = installed_conformance

    outcome = await installed_run(installed, mode, plugin="fixtures_replaced")

    assert not outcome.result.accepted
    assert outcome.result.completed == 0
    assert outcome.returncode != 0
    assert outcome.closure.state is CleanupOutcome.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configuration", [{}, {"value": 42}, {"value": "private-invalid", "extra": True}]
)
async def test_installed_conformance_rejects_invalid_input_before_fixtures(
    installed_conformance: InstalledTooling,
    configuration: JsonObject,
) -> None:
    installed = installed_conformance

    outcome = await installed_run(
        installed,
        configuration=configuration,
        plugin="fixture_module_must_not_be_loaded",
    )

    assert outcome.result.phase == "bootstrap"
    assert not outcome.result.accepted
    assert outcome.returncode != 0
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert b"private-invalid" not in outcome.events


@pytest.mark.asyncio
async def test_installed_conformance_drains_each_fixture_phase(
    installed_conformance: InstalledTooling,
) -> None:
    marker = b"private-installed-output-marker"

    outcome = await installed_run(
        installed_conformance, plugin="fixtures_noisy", feedback_phases=b"sct"
    )

    assert outcome.result.accepted, outcome
    assert len(outcome.phase_bytes) == 3
    assert outcome.phase_bytes[0] > 2 * len(marker) * 65536
    assert outcome.phase_bytes[1] > outcome.phase_bytes[0] + len(marker) * 65536
    assert outcome.phase_bytes[2] > outcome.phase_bytes[1] + 2 * len(marker) * 65536
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert marker not in outcome.events
    assert marker.decode() not in outcome.result.model_dump_json()


@pytest.mark.asyncio
async def test_installed_conformance_refuses_warning(
    installed_conformance: InstalledTooling,
) -> None:
    installed = installed_conformance

    outcome = await installed_run(installed, "warning", plugin="fixtures_noisy")

    assert not outcome.result.accepted
    assert outcome.returncode != 0
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert b"private-installed-warning-marker" not in outcome.events


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin", ["fixtures_missing", "fixtures_wrong_bundle"])
async def test_installed_conformance_refuses_missing_adapter_or_wrong_bundle(
    installed_conformance: InstalledTooling,
    plugin: str,
) -> None:
    installed = installed_conformance

    outcome = await installed_run(installed, plugin=plugin)

    assert not outcome.result.accepted
    assert outcome.result.completed == 0
    assert outcome.returncode != 0
    assert outcome.closure.state is CleanupOutcome.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["/", "../borrowed"])
async def test_installed_storage_refuses_unowned_roots(
    installed_conformance: InstalledTooling, root: str
) -> None:
    configuration: JsonObject = {"root": root, "limits": {"version": 1}}

    outcome = await installed_run(
        installed_conformance,
        protocol=ProtocolName.ARTIFACT_STORAGE,
        plugin="fixtures_storage",
        configuration=configuration,
    )

    assert outcome.result.phase == "storage"
    assert outcome.result.completed == 0
    assert not outcome.result.accepted
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert outcome.returncode != 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "plugin", "completed"),
    [
        ("exercise", "fixtures_storage", 4),
        ("factory", "fixtures_replaced", 0),
        ("preexisting", "fixtures_storage", 0),
    ],
)
async def test_installed_storage_retains_failure_until_parent_stop(
    installed_conformance: InstalledTooling, mode: str, plugin: str, completed: int
) -> None:
    configuration: JsonObject = {"root": "store", "limits": {"version": 1}}

    outcome = await installed_run(
        installed_conformance,
        mode,
        protocol=ProtocolName.ARTIFACT_STORAGE,
        plugin=plugin,
        configuration=configuration,
    )

    assert outcome.result.phase == "storage"
    assert outcome.result.completed == completed
    assert not outcome.result.accepted
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert outcome.returncode != 0


@pytest.mark.asyncio
async def test_installed_storage_stops_blocked_native_work_before_tree_release(
    installed_conformance: InstalledTooling,
) -> None:
    configuration: JsonObject = {"root": "store", "limits": {"version": 1}}

    with pytest.raises(ChannelError):
        await installed_run(
            installed_conformance,
            "blocked",
            protocol=ProtocolName.ARTIFACT_STORAGE,
            plugin="fixtures_storage",
            configuration=configuration,
            feedback_phases=b"b",
            stop_on_feedback=True,
        )


@pytest.mark.parametrize("mode", ["outside_link", "substitution", "auxiliary"])
def test_installed_fixture_preserves_unowned_or_unsettled_store(
    installed_conformance: InstalledTooling, mode: str, tmp_path: Path
) -> None:
    command = [
        str(installed_conformance.python),
        "-I",
        "-W",
        "error",
        "-m",
        "kora_tooling_fixture.disposal_checks",
        mode,
    ]

    result = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.asyncio
async def test_installed_vcs_retains_root_until_cancelled_native_work_finishes(
    installed_conformance: InstalledTooling,
) -> None:
    installed = installed_conformance

    outcome = await installed_run(
        installed,
        "cancel_native",
        protocol=ProtocolName.VCS,
        plugin="fixtures_vcs",
        feedback_phases=b"n",
    )

    assert not outcome.result.accepted
    assert outcome.result.completed == 4
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert outcome.returncode != 0
    assert len(outcome.phase_bytes) == 1


@pytest.mark.asyncio
async def test_installed_storage_preserves_parent_stop_after_report_channel_failure(
    installed_conformance: InstalledTooling,
) -> None:
    configuration: JsonObject = {"root": "store", "limits": {"version": 1}}

    with pytest.raises(ChannelError):
        await installed_run(
            installed_conformance,
            "exercise",
            protocol=ProtocolName.ARTIFACT_STORAGE,
            plugin="fixtures_storage",
            configuration=configuration,
            feedback_phases=b"r",
            stop_on_feedback=True,
            entrypoint="kora_tooling_fixture.broken_report",
        )


@pytest.mark.asyncio
async def test_installed_conformance_keeps_imports_isolated(
    installed_conformance: InstalledTooling,
) -> None:
    installed = installed_conformance

    outcome = await installed_run(installed, "isolated")

    assert outcome.result.accepted, outcome
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert outcome.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["valid", "invalid", "large", "fifo", "missing"])
async def test_installed_conformance_reads_only_bounded_regular_input_files(
    installed_conformance: InstalledTooling, tmp_path: Path, content: str
) -> None:
    path = tmp_path / "private-input"
    if content == "fifo":
        os.mkfifo(path, 0o600)
    elif content != "missing":
        path.write_bytes(
            b'{"value":"file-authoritative-value"}'
            if content == "valid"
            else b"x" * 1048577
            if content == "large"
            else b'{"value":'
        )
    files = InputFiles(configuration=str(path))

    outcome = await installed_run(installed_conformance, files=files)

    assert outcome.result.accepted is (content == "valid")
    assert outcome.closure.state is CleanupOutcome.CLOSED
    assert outcome.returncode == (0 if content == "valid" else 1)
    assert b"file-authoritative-value" not in outcome.events
    assert str(path) not in outcome.result.model_dump_json()
