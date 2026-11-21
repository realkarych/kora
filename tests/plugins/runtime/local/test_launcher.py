import asyncio
import errno
import hashlib
import os
import signal
import socket
import sys
import threading
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.harness.claude.adapter import ClaudeHarness
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.plugins.runtime.local.channel import (
    PRE_ATTACH_BYTES,
    FrameType,
    LaunchCapability,
    LaunchExit,
    LaunchFailure,
    LaunchSpecEnvelope,
    LaunchSpecRequest,
    LaunchStarted,
)
from kora.plugins.runtime.local.launch import COMPONENT as LAUNCH_COMPONENT
from kora.plugins.runtime.local.launch import (
    BinaryVerifier,
    ExpectedBinary,
    LocalLaunches,
)
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.serving import ControlServer
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    AgentExecution,
    HarnessAdapter,
    LaunchInputs,
    Placement,
)
from kora.protocols.lifetime import EnvironmentVariable
from kora.protocols.runtime import SessionResultObservation
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock, SystemClock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import AgentHost, HostedHarness, HostServices
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import NativeIdentityReader
from kora.runner.peers import kernel_source
from kora.runner.sink import RunnerSink
from tests.plugins.harness.claude.test_claude_conformance import make_adapter
from tests.plugins.harness.claude.test_replay_events import context_for
from tests.plugins.harness.claude.test_session_lifecycle import session_spec
from tests.plugins.runtime.local.frames_support import StepClock
from tests.runner.agent_support import SteppedClock, make_world
from tests.runner.test_agent_host_refuses_expired_deadline import SECOND, deadline
from tests.support.local_peers import SOCKET_NAME, served, until
from tests.support.local_runners import short_directory

__all__ = ["kora_capture"]

PLATFORM_ENVIRONMENT: Final[frozenset[str]] = (
    frozenset({"__CF_USER_TEXT_ENCODING"}) if sys.platform == "darwin" else frozenset()
)


@pytest.fixture
def launch_root() -> Iterator[Path]:
    with short_directory() as root:
        yield root


@pytest.mark.asyncio
async def test_launcher_probe_needs_no_capability(tmp_path: Path) -> None:
    launcher = tmp_path / "claude"
    launcher.write_text(
        f"#!{sys.executable} -I\n"
        "from kora.plugins.runtime.local.launcher import main\n"
        "raise SystemExit(main())\n"
    )
    launcher.chmod(0o500)

    probe = await asyncio.create_subprocess_exec(
        str(launcher),
        "-v",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    probe_output = await asyncio.wait_for(probe.communicate(), 5)
    refused = await asyncio.create_subprocess_exec(
        str(launcher), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    refused_output = await asyncio.wait_for(refused.communicate(), 5)

    assert probe.returncode == 0
    assert probe_output == (b"", b"")
    assert refused.returncode == 70
    assert refused_output == (b"", b"")
    assert tuple(tmp_path.iterdir()) == (launcher,)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "exec", "environment"])
async def test_launcher_takes_spec_from_private_channel(
    launch_root: Path, failure: Literal["none", "exec", "environment"]
) -> None:
    tmp_path = launch_root
    attempt = tmp_path / "attempt"
    directory = attempt / "control" / "launch" / "one"
    directory.mkdir(parents=True, mode=0o700)
    attempt.chmod(0o700)
    log = attempt / "runner.log"
    log.touch(mode=0o600)
    cwd = attempt / "candidate"
    cwd.mkdir()
    (cwd / "socket.py").write_text("raise RuntimeError('cwd module imported')\n")
    launcher = directory / "claude"
    launcher.write_text(
        f"#!{sys.executable} -I\n"
        "from kora.plugins.runtime.local.launcher import main\n"
        "raise SystemExit(main())\n"
    )
    launcher.chmod(0o500)
    locale = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    executable = tmp_path / "target"
    executable.write_text(
        f"#!{sys.executable} -I\n"
        "import os\n"
        "import sys\n"
        "assert sys.argv[1:] == ['sdk-argument']\n"
        "assert os.getcwd() == os.environ['PWD']\n"
        "assert os.getsid(0) == os.getppid()\n"
        f"platform_environment = {tuple(PLATFORM_ENVIRONMENT)!r}\n"
        "environment = {name: value for name, value in os.environ.items()\n"
        "               if name not in platform_environment}\n"
        f"assert environment == {{'PWD': os.getcwd(), 'LANG': {locale!r}, "
        "'CANARY': 'private-spec-value'}\n"
        "opened = []\n"
        "for name in os.listdir('/dev/fd'):\n"
        "    try:\n"
        "        os.fstat(int(name))\n"
        "    except OSError:\n"
        "        continue\n"
        "    opened.append(int(name))\n"
        "assert sorted(opened) == [0, 1, 2]\n"
        "os.write(1, b'target-complete')\n"
    )
    executable.chmod(0o500)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint = tmp_path / "socket"
    listener.bind(str(endpoint))
    listener.listen(1)
    listener.setblocking(False)
    capability = LaunchCapability(
        launch_id="one", capability="a" * 64, socket_path=str(endpoint)
    )
    capability_file = directory / "capability"
    capability_file.write_text(capability.model_dump_json())
    capability_file.chmod(0o400)
    child = await asyncio.create_subprocess_exec(
        str(launcher),
        "sdk-argument",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    connection: socket.socket | None = None

    try:
        async with asyncio.timeout(10):
            connection, _ = await asyncio.get_running_loop().sock_accept(listener)
            clock = SystemClock()
            channel = BoundedChannel(connection, clock)
            deadline = clock.monotonic_ns() + 5_000_000_000
            _, payload = await channel.read_typed_frame(
                deadline, {FrameType.JSON.value: PRE_ATTACH_BYTES}
            )
            request = LaunchSpecRequest.model_validate_json(payload)
            spec = LaunchSpecEnvelope(
                request_id=request.request_id,
                result="launch_spec",
                executable=str(
                    tmp_path / "absent" if failure == "exec" else executable
                ),
                cwd=str(cwd),
                environment=tuple(
                    EnvironmentVariable(name=name, value=value)
                    for name, value in {
                        "PWD": str(cwd) if failure != "environment" else "/wrong",
                        "LANG": locale,
                        "CANARY": "private-spec-value",
                    }.items()
                ),
            )
            await channel.write_typed_frame(
                FrameType.JSON, spec.model_dump_json().encode(), deadline
            )
            if failure != "none":
                _, payload = await channel.read_typed_frame(
                    deadline, {FrameType.JSON.value: PRE_ATTACH_BYTES}
                )
                assert LaunchFailure.model_validate_json(payload).op == "launch_failed"
            else:
                _, payload = await channel.read_typed_frame(
                    deadline, {FrameType.JSON.value: PRE_ATTACH_BYTES}
                )
                assert LaunchStarted.model_validate_json(payload).pgid == child.pid
                _, payload = await channel.read_typed_frame(
                    deadline, {FrameType.JSON.value: PRE_ATTACH_BYTES}
                )
                assert LaunchExit.model_validate_json(payload).exit_code == 0
                channel.close()
            output, errors = await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        if connection is not None:
            connection.close()
        listener.close()

    assert request.launch_id == capability.launch_id
    assert request.capability == capability.capability
    assert child.returncode == {"none": 0, "exec": 72, "environment": 72}[failure], (
        errors
    )
    assert output == (b"target-complete" if failure == "none" else b"")
    assert errors == b""
    assert all(
        b"private-spec-value" not in path.read_bytes()
        for path in attempt.rglob("*")
        if path.is_file()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending", ["exit", "signal", "sigkill", "digest", "spawn", "kill"]
)
async def test_launcher_reports_and_releases_resources(
    launch_root: Path, kora_capture: Capture, ending: str
) -> None:
    target = launch_root / "target"
    target.write_text(
        f"#!{sys.executable} -I\nimport os\nimport signal\n"
        + {
            "exit": "raise SystemExit(17)\n",
            "signal": "os.kill(os.getpid(), signal.SIGTERM)\n",
            "sigkill": "os.kill(os.getpid(), signal.SIGKILL)\n",
            "digest": "raise SystemExit(99)\n",
            "spawn": "raise SystemExit(99)\n",
            "kill": "os.write(1, b'ready')\nos.read(0, 1)\n",
        }[ending]
    )
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    if ending == "digest":
        target.write_text(
            target.read_text().replace("SystemExit(99)", "SystemExit(98)")
        )
    target.chmod(0o400 if ending == "spawn" else 0o500)
    assert binary.matches() is (ending != "digest")
    async with launch_world(launch_root, kora_capture, binary) as (launches, server):
        for _ in range(3):
            launch = await launches.allocate(LaunchInputs({}, launch_root))
            child = await asyncio.create_subprocess_exec(
                str(launch.cli_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                if ending == "kill":
                    assert child.stdout is not None
                    assert (
                        await asyncio.wait_for(child.stdout.readexactly(5), 5)
                        == b"ready"
                    )
                    await asyncio.wait_for(launch.ready.wait(), 5)
                    await launch.kill()
                outcome = await asyncio.wait_for(launch.ended(), 10)
                output, errors = await asyncio.wait_for(child.communicate(), 10)
                await launch.kill()
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

            expected = {
                "exit": ("cli_exit_without_result", 17, None),
                "signal": ("launcher_died", None, signal.SIGTERM),
                "sigkill": ("launcher_died", None, signal.SIGKILL),
                "digest": ("integrity_mismatch", None, None),
                "spawn": ("launch_failed", None, None),
                "kill": ("launcher_died", None, None),
            }[ending]
            assert (outcome.cause, outcome.exit_code, outcome.signal) == expected
            assert not launch.cli_path.parent.exists()
            assert launches.pending == {}
            assert launches.held == set()
            assert output == b""
            assert errors == b""
            if ending == "sigkill":
                assert child.returncode == -signal.SIGKILL
        await until(lambda: server.connections == 0)
        assert server.connections == 0
    events = [
        item
        for item in kora_capture.finish()
        if item.get("event") == "runtime.local.launcher.ended"
    ]
    assert len(events) == 3


@asynccontextmanager
async def launch_table(
    root: Path,
    capture: Capture,
    binary: BinaryVerifier,
    clock: Clock,
    *,
    placements: Mapping[Placement, Path] | None = None,
) -> AsyncGenerator[LocalLaunches]:
    control = root / "control"
    control.mkdir(mode=0o700)
    anchor = DirectoryAnchor.open_path(control)
    scope = SecretScope(capture.diagnostics)
    reaper = AdoptedReaper()
    launches = LocalLaunches(
        {} if placements is None else placements,
        anchor,
        control,
        root / SOCKET_NAME,
        binary,
        scope.admit("launch-test-token"),
        clock,
        NativeIdentityReader(),
        capture.diagnostics.logger(LAUNCH_COMPONENT),
        reaper,
        {},
    )
    try:
        yield launches
    finally:
        try:
            await launches.close()
        finally:
            await reaper.stop()
            anchor.close()
            scope.release()


@asynccontextmanager
async def launch_world(
    root: Path, capture: Capture, binary: ExpectedBinary, clock: Clock | None = None
) -> AsyncGenerator[tuple[LocalLaunches, ControlServer]]:
    clock = SystemClock() if clock is None else clock
    async with (
        launch_table(root, capture, binary, clock) as launches,
        served(
            root, clock, kernel_source(NativeIdentityReader()), capture, launches
        ) as (
            server,
            _,
        ),
    ):
        yield launches, server


def pid_absent(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def wait_pid_absent(pid: int) -> bool:
    def absent_or_reaped() -> bool:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        return pid_absent(pid)

    await until(absent_or_reaped)
    return True


@pytest.mark.asyncio
async def test_launcher_escalates_when_target_ignores_sigterm(
    launch_root: Path, kora_capture: Capture
) -> None:
    target = launch_root / "target"
    recorded_pid = launch_root / "target.pid"
    target.write_text(
        f"#!{sys.executable} -I\n"
        "import os\nimport signal\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(recorded_pid)!r}).write_text(str(os.getpid()))\n"
        "os.write(1, b'ready')\n"
        "while True:\n    signal.pause()\n"
    )
    target.chmod(0o500)
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    async with launch_world(launch_root, kora_capture, binary) as (launches, _):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        child = await asyncio.create_subprocess_exec(
            str(launch.cli_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        target_pid: int | None = None
        try:
            assert child.stdout is not None
            assert await asyncio.wait_for(child.stdout.readexactly(5), 5) == b"ready"
            target_pid = int(recorded_pid.read_text())
            await asyncio.wait_for(launch.ready.wait(), 5)

            child.send_signal(signal.SIGTERM)
            returncode = await asyncio.wait_for(child.wait(), 15)
            outcome = await asyncio.wait_for(launch.ended(), 15)
            target_gone = await wait_pid_absent(target_pid)

            assert returncode == -signal.SIGKILL
            assert outcome.cause == "launcher_died"
            assert outcome.exit_code is None
            assert outcome.signal is None
            assert pid_absent(child.pid)
            assert target_gone
            assert pid_absent(target_pid)
        finally:
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGKILL)
                await child.wait()
            if target_pid is not None and not pid_absent(target_pid):
                try:
                    os.kill(target_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.asyncio
async def test_launcher_connect_timeout_revokes_and_signals_child(
    launch_root: Path, kora_capture: Capture
) -> None:
    clock = StepClock()
    binary = ExpectedBinary(Path("/usr/bin/false"), "0" * 64, 0)
    async with launch_world(launch_root, kora_capture, binary, clock) as (launches, _):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        launch.cli_path.chmod(0o700)
        launch.cli_path.write_text(
            f"#!{sys.executable} -I\nimport os\nos.write(1, b'ready')\nos.read(0, 1)\n"
        )
        child = await asyncio.create_subprocess_exec(
            str(launch.cli_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            assert child.stdout is not None
            await asyncio.wait_for(child.stdout.readexactly(5), 5)
            clock.advance(9_999_999_999)
            assert not launch.result.done()

            clock.advance(1)
            outcome = await asyncio.wait_for(launch.ended(), 5)
            returncode = await asyncio.wait_for(child.wait(), 5)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

        assert outcome.cause == "launch_failed"
        assert returncode == -signal.SIGTERM
        assert not launch.cli_path.parent.exists()


@pytest.mark.asyncio
async def test_launcher_refuses_environment_owner_collision(
    launch_root: Path, kora_capture: Capture
) -> None:
    binary = ExpectedBinary(Path("/usr/bin/false"), "0" * 64, 0)
    async with launch_world(launch_root, kora_capture, binary) as (launches, _):
        with pytest.raises(KoraProtocolError) as caught:
            await launches.allocate(LaunchInputs({"PATH": "/untrusted"}, launch_root))

        assert caught.value.code == "launch_rejected"
        assert tuple(launches.path.iterdir()) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "peer", ["foreign_parent", "foreign_session", "leader", "foreign_argv"]
)
async def test_control_channel_refuses_launcher_impostors(
    launch_root: Path, kora_capture: Capture, peer: str
) -> None:
    marker = launch_root / "executed"
    target = launch_root / "target"
    target.write_text(
        f"#!{sys.executable} -I\n"
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    target.chmod(0o500)
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    async with launch_world(launch_root, kora_capture, binary) as (launches, _):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        argv = [str(launch.cli_path)]
        if peer == "foreign_parent":
            argv = [
                sys.executable,
                "-I",
                "-c",
                "import subprocess, sys\n"
                "raise SystemExit(subprocess.call(sys.argv[1:]))",
                str(launch.cli_path),
            ]
        if peer == "foreign_argv":
            argv = [
                sys.executable,
                "-I",
                "-c",
                "import sys\n"
                "from kora.plugins.runtime.local.launcher import main\n"
                "raise SystemExit(main(sys.argv[1:]))",
                str(launch.cli_path),
            ]

        child = await asyncio.create_subprocess_exec(
            *argv,
            start_new_session=peer == "foreign_session",
            process_group=0 if peer == "leader" else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output = await asyncio.wait_for(child.communicate(), 10)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

        assert child.returncode == 72
        assert output == (b"", b"")
        assert launch.identity is None
        assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_launcher_refuses_reused_or_expired_capability(
    launch_root: Path, kora_capture: Capture, expired: bool
) -> None:
    clock = StepClock()
    binary = ExpectedBinary(Path("/usr/bin/false"), "0" * 64, 0)
    async with launch_world(launch_root, kora_capture, binary, clock) as (launches, _):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        capability = (launch.cli_path.parent / "capability").read_bytes()
        entry = launch.cli_path.read_bytes()
        if expired:
            clock.advance(10_000_000_000)
        else:
            first = await asyncio.create_subprocess_exec(str(launch.cli_path))
            await asyncio.wait_for(first.wait(), 10)
        outcome = await asyncio.wait_for(launch.ended(), 10)
        launch.cli_path.parent.mkdir(mode=0o700)
        launch.cli_path.write_bytes(entry)
        launch.cli_path.chmod(0o500)
        secret = launch.cli_path.parent / "capability"
        secret.write_bytes(capability)
        secret.chmod(0o400)

        child = await asyncio.create_subprocess_exec(
            str(launch.cli_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output = await asyncio.wait_for(child.communicate(), 10)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            secret.unlink()
            launch.cli_path.unlink()
            launch.cli_path.parent.rmdir()

        assert outcome.cause == ("launch_failed" if expired else "integrity_mismatch")
        assert child.returncode == 72
        assert output == (b"", b"")


@pytest.mark.asyncio
async def test_launcher_connection_survives_idle_deadline(
    launch_root: Path, kora_capture: Capture
) -> None:
    clock = StepClock()
    target = launch_root / "target"
    target.write_text(
        f"#!{sys.executable} -I\nimport os\nos.write(1, b'ready')\nos.read(0, 1)\n"
    )
    target.chmod(0o500)
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    async with launch_world(launch_root, kora_capture, binary, clock) as (launches, _):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        child = await asyncio.create_subprocess_exec(
            str(launch.cli_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            assert child.stdout is not None
            assert child.stdin is not None
            await asyncio.wait_for(child.stdout.readexactly(5), 10)
            await asyncio.wait_for(launch.ready.wait(), 10)

            clock.advance(6_000_000_000)
            child.stdin.write(b"x")
            await child.stdin.drain()
            outcome = await asyncio.wait_for(launch.ended(), 10)
            code = await asyncio.wait_for(child.wait(), 10)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

        assert outcome.cause == "cli_exit_without_result"
        assert code == -signal.SIGKILL


@pytest.mark.asyncio
async def test_launcher_kill_before_started_only_signals_launcher(
    launch_root: Path, kora_capture: Capture
) -> None:
    binary = ExpectedBinary(
        Path("/usr/bin/false"),
        hashlib.sha256(Path("/usr/bin/false").read_bytes()).hexdigest(),
        Path("/usr/bin/false").stat().st_size,
    )
    async with launch_world(launch_root, kora_capture, binary) as (launches, _):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        launch.cli_path.chmod(0o700)
        launch.cli_path.write_text(
            f"#!{sys.executable} -I\n"
            "import asyncio\nimport os\nimport socket\nimport sys\n"
            "from pathlib import Path\n"
            "from kora.runner.filesystem import DirectoryAnchor\n"
            "from kora.plugins.runtime.local.launcher import capability_at, receive\n"
            "directory = DirectoryAnchor.open_path(Path(sys.argv[0]).parent)\n"
            "capability = capability_at(directory)\ndirectory.close()\n"
            "connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "spec = asyncio.run(receive(connection, capability))\n"
            "os.write(1, b'ready')\nos.read(0, 1)\n"
        )
        child = await asyncio.create_subprocess_exec(
            str(launch.cli_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            assert child.stdout is not None
            await asyncio.wait_for(child.stdout.readexactly(5), 10)
            assert not launch.started

            await launch.kill()
            outcome = await asyncio.wait_for(launch.ended(), 10)
            code = await asyncio.wait_for(child.wait(), 10)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

        assert outcome.cause == "launcher_died"
        assert code == -signal.SIGTERM


@pytest.mark.asyncio
async def test_terminal_report_stops_remaining_group_child(
    launch_root: Path, kora_capture: Capture
) -> None:
    target = launch_root / "target"
    recorded_pid = launch_root / "descendant.pid"
    target.write_text(
        f"#!{sys.executable} -I\n"
        "import subprocess\nimport sys\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-I', '-c', "
        "'import signal; signal.pause()'])\n"
        f"Path({str(recorded_pid)!r}).write_text(str(child.pid))\n"
    )
    target.chmod(0o500)
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    async with launch_world(launch_root, kora_capture, binary) as (launches, server):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        child = await asyncio.create_subprocess_exec(str(launch.cli_path))
        descendant: int | None = None
        try:
            outcome = await asyncio.wait_for(launch.ended(), 15)
            descendant = int(recorded_pid.read_text())
            await asyncio.wait_for(child.wait(), 15)
            await wait_pid_absent(descendant)
            await until(lambda: server.connections == 0)

            assert outcome.cause == "cli_exit_without_result"
            assert outcome.exit_code == 0
            assert pid_absent(descendant)
            assert not launch.cli_path.parent.exists()
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            if descendant is not None and not pid_absent(descendant):
                try:
                    os.kill(descendant, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.asyncio
async def test_close_finishes_started_and_unconnected_launches(
    launch_root: Path, kora_capture: Capture
) -> None:
    target = launch_root / "target"
    target.write_text(
        f"#!{sys.executable} -I\nimport os\n"
        "os.write(1, str(os.getpid()).encode() + b'\\n')\nos.read(0, 1)\n"
    )
    target.chmod(0o500)
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    async with launch_world(launch_root, kora_capture, binary) as (launches, server):
        started = await launches.allocate(LaunchInputs({}, launch_root))
        unconnected = await launches.allocate(LaunchInputs({}, launch_root))
        child = await asyncio.create_subprocess_exec(
            str(started.cli_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            assert child.stdout is not None
            target_pid = int(await asyncio.wait_for(child.stdout.readline(), 15))
            await asyncio.wait_for(started.ready.wait(), 15)

            await asyncio.wait_for(launches.close(), 15)
            started_end = await started.ended()
            unconnected_end = await unconnected.ended()
            await asyncio.wait_for(child.wait(), 15)
            await wait_pid_absent(target_pid)
            await until(lambda: server.connections == 0 and not launches.tasks)

            assert started_end.cause == "launcher_died"
            assert unconnected_end.cause == "launch_failed"
            assert launches.pending == {}
            assert not started.cli_path.parent.exists()
            assert not unconnected.cli_path.parent.exists()
            assert pid_absent(child.pid)
            assert pid_absent(target_pid)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()


@pytest.mark.asyncio
async def test_recorded_launcher_cannot_claim_another_capability(
    launch_root: Path, kora_capture: Capture
) -> None:
    target = Path("/usr/bin/false")
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    async with launch_world(launch_root, kora_capture, binary) as (launches, server):
        first = await launches.allocate(LaunchInputs({}, launch_root))
        second = await launches.allocate(LaunchInputs({}, launch_root))
        first.cli_path.chmod(0o700)
        first.cli_path.write_text(
            f"#!{sys.executable} -I\n"
            "import asyncio\nimport os\nimport socket\nfrom pathlib import Path\n"
            "from kora.runner.filesystem import DirectoryAnchor\n"
            "from kora.runner.channels import ChannelError\n"
            "from kora.plugins.runtime.local.launcher import capability_at, receive\n"
            f"one = DirectoryAnchor.open_path(Path({str(first.cli_path.parent)!r}))\n"
            f"two = DirectoryAnchor.open_path(Path({str(second.cli_path.parent)!r}))\n"
            "first = capability_at(one)\nsecond = capability_at(two)\n"
            "one.close()\ntwo.close()\n"
            "a = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "b = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "asyncio.run(receive(a, first))\n"
            "try:\n    asyncio.run(receive(b, second))\n"
            "except ChannelError:\n    os.write(1, b'refused')\n"
            "else:\n    raise SystemExit(99)\n"
            "b.close()\nos.read(0, 1)\na.close()\n"
        )
        child = await asyncio.create_subprocess_exec(
            str(first.cli_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            assert child.stdout is not None

            response = await asyncio.wait_for(child.stdout.readexactly(7), 15)

            assert response == b"refused"
            assert child.pid in launches.held
            assert first.identity is not None
            assert second.identity is None
            assert second.capability is not None
            assert (second.cli_path.parent / "capability").exists()
            await first.kill()
            await second.kill()
            await asyncio.wait_for(first.ended(), 15)
            await asyncio.wait_for(child.wait(), 15)
            await until(lambda: server.connections == 0)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()


@pytest.mark.asyncio
async def test_launcher_reports_missing_binary_as_integrity_mismatch(
    launch_root: Path, kora_capture: Capture
) -> None:
    binary = ExpectedBinary(launch_root / "absent", "0" * 64, 1)
    async with launch_world(launch_root, kora_capture, binary) as (launches, _):
        launch = await launches.allocate(LaunchInputs({}, launch_root))
        child = await asyncio.create_subprocess_exec(
            str(launch.cli_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output = await asyncio.wait_for(child.communicate(), 10)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        outcome = await asyncio.wait_for(launch.ended(), 10)

    assert outcome.cause == "integrity_mismatch"
    assert child.returncode == 72
    assert output == (b"", b"")
    assert not launch.cli_path.parent.exists()


@pytest.mark.asyncio
async def test_allocate_refuses_relative_cwd_and_excess_launches(
    launch_root: Path, kora_capture: Capture
) -> None:
    binary = ExpectedBinary(Path("/usr/bin/false"), "0" * 64, 0)
    async with launch_world(launch_root, kora_capture, binary) as (launches, _):
        with pytest.raises(KoraProtocolError) as relative:
            await launches.allocate(LaunchInputs({}, Path("candidate")))
        admitted = [
            await launches.allocate(LaunchInputs({}, launch_root)) for _ in range(4)
        ]
        with pytest.raises(KoraProtocolError) as excess:
            await launches.allocate(LaunchInputs({}, launch_root))
        for launch in admitted:
            await launch.kill()
        outcomes = [await launch.ended() for launch in admitted]

    assert relative.value.code == "launch_rejected"
    assert excess.value.code == "launch_rejected"
    assert {outcome.cause for outcome in outcomes} == {"launch_failed"}
    assert launches.pending == {}


class GatedBinaryVerifier:
    def __init__(self, path: Path, loop: asyncio.AbstractEventLoop) -> None:
        self.path = path
        self.loop = loop
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def matches(self) -> bool:
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            if not self.release.wait(5):
                raise TimeoutError("verification was not released")
            return True
        finally:
            self.finished.set()


@dataclass(frozen=True)
class StartupResolver:
    adapter: ClaudeHarness

    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness:
        return HostedHarness(cast(HarnessAdapter, self.adapter), self.adapter.kill)


def startup_target(root: Path) -> tuple[ExpectedBinary, Path]:
    started = root / "target-started"
    executable = root / "target"
    script = (
        f"#!{sys.executable} -I\n"
        "from pathlib import Path\n"
        f"Path({str(started)!r}).touch()\n"
    ).encode()
    executable.write_bytes(script)
    executable.chmod(0o500)
    return ExpectedBinary(
        executable, hashlib.sha256(script).hexdigest(), len(script)
    ), started


@pytest.mark.asyncio
async def test_claude_deadline_refuses_native_allocation_during_verification(
    launch_root: Path, kora_capture: Capture
) -> None:
    world = make_world(launch_root / "host", kora_capture)
    clock = SteppedClock()
    binary, candidate_started = startup_target(launch_root)
    verifier = GatedBinaryVerifier(binary.path, asyncio.get_running_loop())
    ctx = context_for(kora_capture, clock)
    adapter = make_adapter(Path(world.candidate), ctx)
    async with launch_table(
        launch_root,
        kora_capture,
        verifier,
        clock,
        placements={
            "harness_home": launch_root,
            "harness_tmp": launch_root,
            "harness_config": launch_root,
        },
    ) as launches:
        services = HostServices(
            StartupResolver(adapter),
            AllowDenyPolicy(),
            ctx,
            kora_capture.diagnostics.registry,
            clock,
            kora_capture.diagnostics.logger("runner.agent"),
            launches.bind,
        )
        host = AgentHost(services, world.journal, world.control, world.candidate)
        workload = world.workload(
            deadline=deadline(1),
            session_spec=session_spec(adapter).model_dump(mode="json"),
        )

        try:
            async with asyncio.timeout(4):
                host.start(workload, world.workspace())
                await verifier.entered.wait()
                await clock.requested(SECOND)
                await clock.advance_to(SECOND)
                await host.finished()
                observation = host.observation()
                verified_while_stopping = verifier.finished.is_set()
                launch_files = tuple(launches.path.iterdir())
                released = await host.stop()
                await launches.close()
        finally:
            verifier.release.set()
            await asyncio.to_thread(verifier.finished.wait, 3)
            await host.stop()
            world.close()

    assert isinstance(observation, SessionResultObservation)
    assert observation.cause == "deadline"
    assert observation.result.stop_reason == "deadline"
    assert not verified_while_stopping
    assert launch_files == ()
    assert not candidate_started.exists()
    assert released.session
    assert released.writer
    assert not launches.pending
    assert not launches.tasks
    assert launches.allocating == 0
    assert launches.directory.closed
    assert verifier.finished.is_set()


@pytest.mark.asyncio
async def test_launcher_refuses_partial_specification_after_runner_stop(
    launch_root: Path, kora_capture: Capture
) -> None:
    world = make_world(launch_root / "host", kora_capture)
    clock = SteppedClock()
    binary, candidate_started = startup_target(launch_root)
    sink = RunnerSink(
        world.journal, kora_capture.diagnostics.registry, clock, None, 30 * SECOND
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        async with launch_table(launch_root, kora_capture, binary, clock) as launches:
            listener.bind(str(launches.socket_path))
            listener.listen(1)
            listener.setblocking(False)
            peer_source = kernel_source(NativeIdentityReader())
            launch = await launches.bind(sink).allocate(
                LaunchInputs(
                    {f"LARGE_{index}": "x" * 30_000 for index in range(4)}, launch_root
                )
            )
            child = await asyncio.create_subprocess_exec(
                str(launch.cli_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                async with asyncio.timeout(10):
                    connection, _ = await asyncio.get_running_loop().sock_accept(
                        listener
                    )
                    with connection:
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
                        peer = peer_source.attest(connection, peer_source.stamp())
                        channel = BoundedChannel(connection, clock)
                        _, payload = await channel.read_typed_frame(
                            7 * SECOND, {FrameType.JSON.value: PRE_ATTACH_BYTES}
                        )
                        request = LaunchSpecRequest.model_validate_json(payload)
                        child.send_signal(signal.SIGSTOP)
                        _, status = await asyncio.to_thread(
                            os.waitpid, child.pid, os.WUNTRACED
                        )
                        assert os.WIFSTOPPED(status)
                        async with asyncio.TaskGroup() as tasks:
                            tasks.create_task(launches.serve(request, peer, channel))
                            await clock.requested(5 * SECOND)
                            await asyncio.to_thread(
                                sink.terminate, "journal_unavailable"
                            )
                            cause = await sink.termination()
                            child.send_signal(signal.SIGCONT)
                            output, errors = await child.communicate()
                        ending = await launch.ended()
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
                sink.close()
                try:
                    assert await asyncio.wait_for(sink.joined(5 * SECOND), 3)
                finally:
                    world.close()

    assert cause == "journal_unavailable"
    assert (output, errors) == (b"", b"")
    assert not candidate_started.exists()
    assert child.returncode == 72
    assert not launch.ready.is_set()
    assert ending.cause == "launcher_died"
    assert not launch.cli_path.parent.exists()
    assert not launches.pending
    assert not launches.held
    assert not launches.tasks
    assert launches.directory.closed


@pytest.mark.asyncio
async def test_cancelling_allocate_does_not_wait_for_binary_verification(
    tmp_path: Path, kora_capture: Capture
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    anchor = DirectoryAnchor.open_path(control)
    scope = SecretScope(kora_capture.diagnostics)
    verifier = GatedBinaryVerifier(tmp_path / "binary", asyncio.get_running_loop())
    reaper = AdoptedReaper()
    launches = LocalLaunches(
        {},
        anchor,
        control,
        tmp_path / "socket",
        verifier,
        scope.admit("verification-token"),
        SystemClock(),
        NativeIdentityReader(),
        kora_capture.diagnostics.logger(LAUNCH_COMPONENT),
        reaper,
        {},
    )
    allocation = asyncio.create_task(
        launches.allocate(LaunchInputs(environment={}, cwd=tmp_path))
    )
    try:
        await asyncio.wait_for(verifier.entered.wait(), 2)

        allocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(allocation, 0.5)
        blocked = not verifier.release.is_set() and not verifier.finished.is_set()
        verification = launches.verification
        owned = verification is not None and not verification.done()
        verifier.release.set()
        assert await asyncio.to_thread(verifier.finished.wait, 2)
        assert verification is not None
        await asyncio.wait_for(asyncio.shield(verification), 2)
        await asyncio.wait_for(launches.close(), 2)

        assert blocked
        assert owned
        assert verifier.finished.is_set()
        assert verification is not None
        assert verification.done()
        assert verification.result() is True
        assert not launches.pending
        assert not launches.tasks
    finally:
        verifier.release.set()
        allocation.cancel()
        await asyncio.gather(allocation, return_exceptions=True)
        await launches.close()
        await reaper.stop()
        anchor.close()
        scope.release()


@pytest.mark.asyncio
async def test_close_refuses_pending_allocation_without_waiting_for_binary_verification(
    tmp_path: Path, kora_capture: Capture
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    anchor = DirectoryAnchor.open_path(control)
    scope = SecretScope(kora_capture.diagnostics)
    verifier = GatedBinaryVerifier(tmp_path / "binary", asyncio.get_running_loop())
    reaper = AdoptedReaper()
    clock = StepClock()
    launches = LocalLaunches(
        {},
        anchor,
        control,
        tmp_path / "socket",
        verifier,
        scope.admit("verification-token"),
        clock,
        NativeIdentityReader(clock=clock),
        kora_capture.diagnostics.logger(LAUNCH_COMPONENT),
        reaper,
        {},
    )
    allocation = asyncio.create_task(
        launches.allocate(LaunchInputs(environment={}, cwd=tmp_path))
    )
    try:
        await asyncio.wait_for(verifier.entered.wait(), 2)

        await asyncio.wait_for(launches.close(), 0.5)
        with pytest.raises(KoraProtocolError) as refused:
            await asyncio.wait_for(allocation, 0.5)

        assert refused.value.code == "launch_rejected"
        assert not verifier.release.is_set()
        assert not verifier.finished.is_set()
        assert launches.verification is not None
        assert launches.verification.cancelled()
        assert not launches.pending
        assert launches.allocating == 0
        assert not launches.tasks
        assert launches.directory.closed
        with pytest.raises(OSError, match="Bad file descriptor") as closed:
            os.fstat(launches.directory.descriptor)
        assert closed.value.errno == errno.EBADF
        assert clock.offset_ns == 0
    finally:
        verifier.release.set()
        assert await asyncio.to_thread(verifier.finished.wait, 2)
        allocation.cancel()
        await asyncio.gather(allocation, return_exceptions=True)
        await launches.close()
        await reaper.stop()
        anchor.close()
        scope.release()
