import asyncio
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.plugins.runtime.local.services import service_cgroup
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.runner.registry import Running
from tests.support.local_peers import until
from tests.support.local_runners import reap_runners, short_directory
from tests.support.local_runtime import prepared, runtime_context, state_root

__all__ = ["kora_capture"]


def command(arguments: list[str]) -> str:
    completed = subprocess.run(
        arguments, capture_output=True, text=True, check=True, timeout=10
    )
    return completed.stdout.strip()


def session_request(directory: Path, arguments: tuple[str, ...]) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(directory / "control.sock"))
        client.sendall(("\0".join(arguments) + "\n").encode())
        with client.makefile("rb") as response:
            return response.readline(65537).decode().strip()


def stop_user_unit(unit: str) -> None:
    command(["/usr/bin/systemctl", "--user", "stop", "--", unit])
    state = command(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            "--property=ActiveState",
            "--value",
            "--",
            unit,
        ]
    )
    assert state in ("inactive", "failed")


@pytest.mark.asyncio
async def test_service_fixture_owns_its_worker_and_preserves_runner(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ready = tmp_path / "worker-ready.json"
    reader = NativeIdentityReader()
    entry: Running | None = None
    with short_directory() as temporary:
        broker = temporary / "broker"
        broker.mkdir(mode=0o700)
        with (tmp_path / "broker.log").open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.support.service_session", str(broker)],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            await until(lambda: (broker / "control.sock").exists())
            worker = int(
                await asyncio.to_thread(
                    session_request,
                    broker,
                    (str(root), str(temporary / "sockets"), str(ready)),
                )
            )
            await until(ready.exists)
            entry = Running.model_validate_json(ready.read_bytes())

            response = await asyncio.to_thread(session_request, broker, ("close",))
            result = await asyncio.to_thread(process.wait, 10)
            runtime = await LocalRuntime.open(
                LocalRuntimeConfiguration(state_root=str(root)),
                runtime_context(kora_capture),
                socket_directory=temporary / "new",
            )
            try:
                status = await runtime.status(
                    runtime.registry.key("service-attempt").handle(),
                    runtime_context(kora_capture),
                )
            finally:
                await runtime.close()

            assert response == "closing"
            assert result == 0
            assert status.state == "starting"
            assert reader.read(worker).state is IdentityState.ABSENT
            assert reader.read(entry.runner.pid).state is IdentityState.PRESENT
            assert not (broker / "control.sock").exists()
        finally:
            if process.poll() is None:
                if (broker / "control.sock").exists():
                    await asyncio.to_thread(session_request, broker, ("close",))
                else:
                    process.terminate()
                await asyncio.to_thread(process.wait, 10)
            reap_runners(root)
            if entry is not None:
                await until(
                    lambda: reader.read(entry.runner.pid).state is IdentityState.ABSENT
                )
                Path(entry.socket_path).unlink(missing_ok=True)


@pytest.mark.host_fixture
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ("restart", "session") if sys.platform == "linux" else ("nonlinux",)
)
async def test_runner_survives_worker_service_restart(
    tmp_path: Path, kora_capture: Capture, mode: str
) -> None:
    if mode == "nonlinux":
        assert service_cgroup(sys.platform) is None
        return
    root = state_root(tmp_path)
    ready = tmp_path / "service-ready.json"
    unit = f"kora-service-test-{os.getpid()}.service"
    session_directory = prepared("service-session") if mode == "session" else None
    reader = NativeIdentityReader()
    entry: Running | None = None
    worker: int | None = None
    session_closed = False
    with short_directory() as temporary:
        sockets = temporary / "original"
        try:
            if session_directory is not None:
                worker = int(
                    await asyncio.to_thread(
                        session_request,
                        session_directory,
                        (str(root), str(sockets), str(ready), "linger-false"),
                    )
                )
            else:
                await asyncio.to_thread(
                    command,
                    [
                        "/usr/bin/systemd-run",
                        "--user",
                        f"--unit={unit}",
                        f"--working-directory={Path.cwd()}",
                        "--property=KillMode=process",
                        "--property=PrivateTmp=no",
                        "--property=OOMPolicy=continue",
                        "--property=RuntimeMaxSec=120s",
                        "--property=TimeoutStopSec=5s",
                        "--",
                        sys.executable,
                        "-m",
                        "tests.support.service_worker",
                        str(root),
                        str(sockets),
                        str(ready),
                    ],
                )
            await until(ready.exists)
            entry = Running.model_validate_json(ready.read_bytes())
            if session_directory is not None:
                assert Path(entry.socket_path).parent == Path(
                    "/", "tmp", f"kora-{os.getuid()}"
                )
            original = reader.read(entry.runner.pid)
            assert original.state is IdentityState.PRESENT

            if session_directory is not None:
                assert (
                    await asyncio.to_thread(
                        session_request, session_directory, ("close",)
                    )
                    == "closing"
                )
                session_closed = True
                scope = prepared("service-session.scope").read_text().strip()
                session_id = scope.removeprefix("session-").removesuffix(".scope")
                await until(
                    lambda: (
                        command(
                            [
                                "/usr/bin/loginctl",
                                "show-session",
                                session_id,
                                "--property=State",
                                "--value",
                            ]
                        )
                        == "closing"
                    )
                )
            else:
                ready.unlink()
                await asyncio.to_thread(
                    command, ["/usr/bin/systemctl", "--user", "restart", "--", unit]
                )
                await until(ready.exists)
                restarted = Running.model_validate_json(ready.read_bytes())
                assert restarted.runner == entry.runner
                assert restarted.socket_path == entry.socket_path
            runtime = await LocalRuntime.open(
                LocalRuntimeConfiguration(state_root=str(root)),
                runtime_context(kora_capture),
                socket_directory=None,
            )
            try:
                assert runtime.service_report is not None
                assert runtime.service_report.socket_directory == Path(
                    f"/run/user/{os.getuid()}/kora"
                )
                status = await runtime.status(
                    runtime.registry.key("service-attempt").handle(),
                    runtime_context(kora_capture),
                )
            finally:
                await runtime.close()

            assert status.state == "starting"
            assert reader.read(entry.runner.pid) == original
            assert Path(entry.socket_path).is_socket()
            if worker is not None:
                await until(lambda: reader.read(worker).state is IdentityState.ABSENT)
        finally:
            if session_directory is None:
                await asyncio.to_thread(stop_user_unit, unit)
            elif not session_closed and (session_directory / "control.sock").exists():
                await asyncio.to_thread(session_request, session_directory, ("close",))
            reap_runners(root)
            if entry is not None:
                await until(
                    lambda: reader.read(entry.runner.pid).state is IdentityState.ABSENT
                )
                Path(entry.socket_path).unlink(missing_ok=True)


@pytest.mark.host_fixture
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        ("KillMode=control-group", "kill_mode"),
        ("KillMode=mixed", "kill_mode"),
        ("PrivateTmp=yes", "private_tmp"),
        ("OOMPolicy=kill", "oom_policy"),
        ("ManagedOOMMemoryPressure=kill", "managed_oom"),
        ("ManagedOOMSwap=kill", "managed_oom"),
    ]
    if sys.platform == "linux"
    else [("nonlinux", "nonlinux")],
)
async def test_service_startup_refuses_real_unit_settings(
    setting: str, reason: str
) -> None:
    if setting == "nonlinux":
        assert service_cgroup(sys.platform) is None
        return
    with tempfile.TemporaryDirectory(
        prefix="kora-service-refusal-", dir=Path.home()
    ) as directory:
        root = state_root(Path(directory))
        result = Path(directory) / "startup-result"
        unit = f"kora-refusal-{os.getpid()}.service"
        arguments = [
            "/usr/bin/systemd-run",
            "--user",
            "--wait",
            "--pipe",
            "--collect",
            f"--unit={unit}",
            f"--working-directory={Path.cwd()}",
            "--property=KillMode=process",
            "--property=PrivateTmp=no",
            "--property=OOMPolicy=continue",
            "--property=RuntimeMaxSec=60s",
            f"--property={setting}",
            f"--property=Description={directory}",
            "--",
            sys.executable,
            "-m",
            "tests.support.service_startup",
            str(root),
            str(result),
        ]

        launching = asyncio.create_task(asyncio.to_thread(command, arguments))
        try:
            await asyncio.shield(launching)

            assert result.read_text() == reason
            assert not (root / "domain.json").exists()
        finally:
            await asyncio.gather(launching, return_exceptions=True)
            description = command(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "show",
                    "--property=Description",
                    "--value",
                    "--",
                    unit,
                ]
            )
            if description == directory:
                try:
                    command(
                        [
                            "/usr/bin/systemctl",
                            "--user",
                            "kill",
                            "--signal=KILL",
                            "--kill-whom=all",
                            "--",
                            unit,
                        ]
                    )
                except subprocess.CalledProcessError:
                    state = command(
                        [
                            "/usr/bin/systemctl",
                            "--user",
                            "show",
                            "--property=ActiveState",
                            "--value",
                            "--",
                            unit,
                        ]
                    )
                    assert state in ("inactive", "failed")
                finally:
                    try:
                        command(["/usr/bin/systemctl", "--user", "stop", "--", unit])
                    except subprocess.CalledProcessError:
                        state = command(
                            [
                                "/usr/bin/systemctl",
                                "--user",
                                "show",
                                "--property=LoadState",
                                "--value",
                                "--",
                                unit,
                            ]
                        )
                        assert state == "not-found"
            state = command(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "show",
                    "--property=LoadState",
                    "--value",
                    "--",
                    unit,
                ]
            )
            assert state == "not-found"


@pytest.mark.asyncio
async def test_service_fixture_termination_closes_worker_before_runner_sweep(
    tmp_path: Path,
) -> None:
    root = state_root(tmp_path)
    reader = NativeIdentityReader()
    with short_directory() as temporary:
        broker = temporary / "broker"
        broker.mkdir(mode=0o700)
        with (tmp_path / "broker.log").open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.support.service_session", str(broker)],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            await until(lambda: (broker / "control.sock").exists())
            worker = int(
                await asyncio.to_thread(
                    session_request,
                    broker,
                    (
                        str(root),
                        str(temporary / "sockets"),
                        str(tmp_path / "ready.json"),
                    ),
                )
            )

            process.terminate()
            result = await asyncio.to_thread(process.wait, 10)

            assert result == 0
            assert reader.read(worker).state is IdentityState.ABSENT
            assert not (broker / "control.sock").exists()
        finally:
            if process.poll() is None:
                if (broker / "control.sock").exists():
                    await asyncio.to_thread(session_request, broker, ("close",))
                else:
                    process.terminate()
                await asyncio.to_thread(process.wait, 10)
            reap_runners(root)
