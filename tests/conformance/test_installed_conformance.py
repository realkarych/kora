import asyncio
import json
import os
import socket
import subprocess
import sys
from collections import deque
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
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import ProbeClock
from tests.support.configured_conformance import (
    installed_conformance as installed_conformance,
)
from tests.support.configured_conformance import vcs_configuration
from tests.support.provider_tooling import (
    InstalledTooling,
)


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
        clock = ProbeClock()
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

                async def cleanup_time() -> None:
                    await owner.closure_started.wait()
                    clock.use_system_time()

                feedback_origin = SystemClock().monotonic_ns()
                feedback_progress: deque[tuple[str, int]] = deque(maxlen=6)

                def mark_feedback(stage: str) -> None:
                    if stop_on_feedback:
                        feedback_progress.append(
                            (stage, SystemClock().monotonic_ns() - feedback_origin)
                        )

                async def feedback() -> tuple[int, ...]:
                    observed: list[int] = []
                    for phase in feedback_phases:
                        endpoint: socket.socket | None = None
                        stage = "accept"
                        try:
                            endpoint, _ = await loop.sock_accept(listeners[3])
                            mark_feedback("accepted")
                            stage = "receive"
                            assert await loop.sock_recv(endpoint, 1) == bytes([phase])
                            mark_feedback("received")
                            observed.append(process.capture_bytes)
                            stage = "acknowledge"
                            await loop.sock_sendall(endpoint, b"a")
                            mark_feedback("acknowledged")
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
                                stage = "close"
                                mark_feedback("close_requested")
                                owner.request_close(None)
                                clock.advance(
                                    duration_ns(owner.limits.cooperative_seconds) + 1
                                )
                                closure = await owner.close(None)
                                mark_feedback("close_reported")
                                assert closure.state is CleanupOutcome.CLOSED
                                assert process.closed
                                assert process.returncode is not None
                                assert invocation.closed
                                assert owner.inventory().retained_trees == 0
                        except asyncio.CancelledError as failure:
                            if stop_on_feedback:
                                control = owner.invocation_cleanup
                                dependencies = owner.recovery_dependencies()[:16]
                                failure.add_note(
                                    repr(
                                        {
                                            "feedback_stage": stage,
                                            "real_offsets_ns": tuple(feedback_progress),
                                            "decision_clock_ns": clock.monotonic_ns(),
                                            "decision_close_deadlines_ns": (
                                                control.start,
                                                control.cooperative,
                                                control.term,
                                                control.final,
                                            ),
                                            "closure_ready": control.result.done(),
                                            "inventory": owner.inventory(),
                                            "owned_frames": tuple(
                                                (
                                                    frame.f_code.co_name[:64],
                                                    frame.f_lineno,
                                                )
                                                for task in dependencies
                                                if isinstance(task, asyncio.Task)
                                                for frame in task.get_stack(limit=1)
                                            ),
                                        }
                                    )
                                )
                            raise
                        finally:
                            if endpoint is not None:
                                endpoint.close()
                    return tuple(observed)

                serving = asyncio.create_task(lifetime())
                reading = asyncio.create_task(events())
                observing = asyncio.create_task(feedback())
                progressing = asyncio.create_task(cleanup_time())
                tasks.extend((serving, reading, observing, progressing))
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
                        mark_feedback("inputs_sent")
                        await asyncio.wait_for(observing, 20)
                    result = ChildResult.model_validate_json(
                        await channel.read_record(deadline, 4096)
                    )
                    if result.phase == "ready":
                        assert result.expected > 0
                        readable = asyncio.create_task(channel.wait_readable())
                        tasks.append(readable)
                        await asyncio.wait(
                            (readable, serving, owner.invocation_cleanup.result),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not readable.done():
                            raise ChannelError(ChannelFailure.CLOSED)
                        readable.result()
                        deadline = SystemClock().monotonic_ns() + 30_000_000_000
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


@pytest.mark.asyncio
async def test_installed_conformance_stops_teardown_without_a_result(
    installed_conformance: InstalledTooling,
) -> None:
    with TemporaryDirectory(prefix="kora-teardown-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        feedback = root / "feedback"
        data = root / "data.json"
        config = root / "config.json"
        config.write_text(json.dumps({"value": "private-secret-value"}))
        data.write_text(
            json.dumps({"mode": "delayed_teardown", "feedback": str(feedback)})
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(feedback))
            listener.listen(1)
            listener.setblocking(False)
            operation = asyncio.create_task(
                installed_run(
                    installed_conformance,
                    plugin="fixtures_command",
                    files=InputFiles(configuration=str(config), fixture_data=str(data)),
                )
            )
            try:
                loop = asyncio.get_running_loop()
                endpoint, _ = await asyncio.wait_for(loop.sock_accept(listener), 30)
                with endpoint:
                    barrier = await asyncio.wait_for(loop.sock_recv(endpoint, 32), 30)
                    assert barrier == b"teardown"

                    with pytest.raises(ChannelError):
                        await asyncio.wait_for(asyncio.shield(operation), 20)

                    assert await asyncio.wait_for(loop.sock_recv(endpoint, 1), 5) == b""
                    assert operation.done()
            finally:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["setup", "bind", "construct", "cleanup", "teardown", "echo"]
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
    "configuration",
    [pytest.param({"value": "private-invalid", "extra": True}, id="extra_property")],
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
