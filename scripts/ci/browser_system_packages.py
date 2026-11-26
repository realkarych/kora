from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYSTEM_CGROUP = Path("/sys/fs/cgroup")
SUDO = "/usr/bin/sudo"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
PYTHON = "/usr/bin/python3"
DPKG = "/usr/bin/dpkg"
START_SECONDS = 5.0
WORK_SECONDS = 105.0
STOP_PHASE_SECONDS = 5
STOP_SECONDS = 2 * STOP_PHASE_SECONDS
PROOF_SECONDS = 5.0
RETURN_SECONDS = 5.0
POLL_SECONDS = 0.25
ADMISSION_BYTES = 256
UNIT_PROPERTIES = (
    "Id,LoadState,Transient,ActiveState,SubState,Job,InvocationID,ControlGroup,"
    "Result,ExecMainCode,ExecMainStatus,Type,ExitType,RemainAfterExit,KillMode,"
    "Restart,Delegate"
)
LIFETIME_PROPERTIES = {
    "Type": "exec",
    "ExitType": "cgroup",
    "RemainAfterExit": "yes",
    "KillMode": "control-group",
    "Restart": "no",
    "Delegate": "no",
}
INVOCATION = re.compile(r"[0-9a-f]{32}")
SERVICE_PATH = re.compile(r"/system.slice/kora-browser-packages-[0-9a-f]{32}\.service")


class BrowserPackagesRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class AttemptWindow:
    start: float
    work: float
    settled: float
    prove: float
    join: float

    @classmethod
    def allocate(cls, allowance: float) -> AttemptWindow:
        now = time.monotonic()
        work = min(WORK_SECONDS, allowance - START_SECONDS - STOP_SECONDS)
        if work <= 0:
            raise BrowserPackagesRefused("Insufficient time to own a package attempt.")
        return cls(
            now + START_SECONDS,
            work,
            now + allowance,
            now + allowance + PROOF_SECONDS,
            now + allowance + PROOF_SECONDS + RETURN_SECONDS,
        )


@dataclass(frozen=True)
class Service:
    name: str
    cgroup: str

    @classmethod
    def new(cls) -> Service:
        name = f"kora-browser-packages-{uuid.uuid4().hex}.service"
        return cls(name, f"/system.slice/{name}")

    async def show(self, deadline: float) -> dict[str, str]:
        code, output = await self.control(
            ("show", "--all", f"--property={UNIT_PROPERTIES}"), deadline
        )
        if code != 0:
            return {}
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    async def control(
        self, arguments: Sequence[str], deadline: float, *, root: bool = False
    ) -> tuple[int, str]:
        prefix = (SUDO, "-n") if root else ()
        async with asyncio.timeout_at(deadline):
            process = await asyncio.create_subprocess_exec(
                *prefix,
                SYSTEMCTL,
                "--system",
                "--no-pager",
                *arguments,
                self.name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await process.communicate()
            finally:
                if process.returncode is None:
                    stop_client(process)
        if process.returncode is None:
            raise BrowserPackagesRefused("Package manager control did not finish.")
        return process.returncode, output.decode("utf-8", "replace")

    async def launch(
        self, work: Sequence[str], window: AttemptWindow
    ) -> asyncio.subprocess.Process:
        async with asyncio.timeout_at(window.start):
            return await asyncio.create_subprocess_exec(
                SUDO,
                "-n",
                SYSTEMD_RUN,
                "--system",
                "--wait",
                "--pipe",
                "--quiet",
                "--expand-environment=no",
                f"--unit={self.name}",
                "--slice=system.slice",
                "--service-type=exec",
                f"--working-directory={ROOT}",
                "--property=ExitType=cgroup",
                "--property=RemainAfterExit=yes",
                "--property=KillMode=control-group",
                "--property=Restart=no",
                "--property=Delegate=no",
                f"--property=TimeoutStartSec={START_SECONDS:g}s",
                f"--property=TimeoutStopSec={STOP_PHASE_SECONDS}s",
                f"--property=RuntimeMaxSec={window.work:.6f}s",
                PYTHON,
                "-I",
                str(Path(__file__).resolve()),
                str(int(window.start * 1_000_000_000)),
                *work,
                stdin=asyncio.subprocess.PIPE,
            )

    async def owned(self, deadline: float) -> str:
        while True:
            status = await self.show(deadline)
            invocation = status.get("InvocationID", "")
            if (
                status.get("Id") == self.name
                and status.get("Transient") == "yes"
                and status.get("ActiveState") == "active"
                and status.get("SubState") == "running"
                and status.get("Job") == ""
                and status.get("ControlGroup") == self.cgroup
                and INVOCATION.fullmatch(invocation)
                and invocation != "0" * 32
            ):
                if any(
                    status.get(key) != value
                    for key, value in LIFETIME_PROPERTIES.items()
                ):
                    raise BrowserPackagesRefused(
                        "Package service lifetime is unsupported."
                    )
                return invocation
            await poll_until(deadline)


@dataclass(frozen=True)
class OriginalService:
    service: Service
    invocation: str
    directory: int

    @classmethod
    def capture(cls, service: Service, invocation: str) -> OriginalService:
        path = SYSTEM_CGROUP / service.cgroup[1:]
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return cls(service, invocation, descriptor)

    def empty(self) -> bool:
        try:
            events = os.open(
                "cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory
            )
        except FileNotFoundError:
            return not (SYSTEM_CGROUP / self.service.cgroup[1:]).exists()
        with os.fdopen(events, "rb") as stream:
            lines = stream.read(1024).splitlines()
        if b"populated 1" in lines:
            return False
        if b"populated 0" not in lines:
            raise BrowserPackagesRefused("Original cgroup state was indeterminate.")
        return True

    async def admit(self, client: asyncio.subprocess.Process, deadline: float) -> None:
        pipe = client.stdin
        if pipe is None or self.empty():
            raise BrowserPackagesRefused("Package admission ownership was lost.")
        async with asyncio.timeout_at(deadline):
            pipe.write(f"{self.invocation} {self.service.cgroup}\n".encode("ascii"))
            await pipe.drain()
        pipe.close()

    async def finished(self, deadline: float) -> bool:
        while True:
            status = await self.service.show(deadline)
            self.require_identity(status)
            if (
                status.get("Job") == ""
                and status.get("SubState") in {"exited", "failed"}
                and self.empty()
            ):
                result = status.get("Result", "")
                code = status.get("ExecMainCode", "")
                exit_status = status.get("ExecMainStatus", "")
                if (
                    not result
                    or code not in {"1", "2", "3"}
                    or not exit_status.isascii()
                    or not exit_status.isdecimal()
                ):
                    raise BrowserPackagesRefused(
                        "Original package result was indeterminate."
                    )
                return result == "success" and code == "1" and exit_status == "0"
            await poll_until(deadline)

    def require_identity(self, status: dict[str, str]) -> None:
        if (
            status.get("Id") != self.service.name
            or status.get("Transient") != "yes"
            or status.get("InvocationID") != self.invocation
            or status.get("ControlGroup") not in {self.service.cgroup, ""}
        ):
            raise BrowserPackagesRefused("Original package service identity was lost.")

    async def settle(self, deadline: float) -> None:
        status = await self.service.show(deadline)
        self.require_identity(status)
        code, _ = await self.service.control(("stop",), deadline, root=True)
        if code != 0 or not self.empty():
            raise BrowserPackagesRefused("Original package service did not settle.")
        after = await self.service.show(deadline)
        if (
            after.get("Id") != self.service.name
            or after.get("Job") != ""
            or after.get("ActiveState") != "inactive"
        ):
            raise BrowserPackagesRefused("Original package service did not settle.")
        if after.get("LoadState") != "not-found":
            self.require_identity(after)


async def poll_until(deadline: float) -> None:
    async with asyncio.timeout_at(deadline):
        await asyncio.sleep(POLL_SECONDS)


def stop_client(client: asyncio.subprocess.Process) -> None:
    try:
        client.kill()
    except (ProcessLookupError, PermissionError):
        return


async def package_attempt(work: Sequence[str], allowance: float) -> str | None:
    window = AttemptWindow.allocate(allowance)
    if not (SYSTEM_CGROUP / "cgroup.controllers").is_file():
        raise BrowserPackagesRefused("Browser packages require cgroup v2 ownership.")
    service = Service.new()
    client = None
    owned = None
    try:
        client = await service.launch(work, window)
        invocation = await service.owned(window.start)
        owned = OriginalService.capture(service, invocation)
        await owned.admit(client, window.start)
        succeeded = await owned.finished(window.settled)
    except (OSError, TimeoutError):
        raise BrowserPackagesRefused(
            "Package attempt ownership was not established."
        ) from None
    finally:
        if client is not None:
            if client.stdin is not None:
                client.stdin.close()
            try:
                if owned is not None:
                    await owned.settle(window.prove)
                async with asyncio.timeout_at(window.join):
                    await client.wait()
            except (OSError, TimeoutError):
                raise BrowserPackagesRefused(
                    "Original package service did not settle."
                ) from None
            finally:
                if owned is not None:
                    os.close(owned.directory)
                if client.returncode is None:
                    stop_client(client)
    return None if succeeded else "original package service failed after settlement"


async def install_browser_system_packages(allowance: float) -> str | None:
    pnpm = shutil.which("pnpm")
    node = shutil.which("node")
    if pnpm is None or node is None:
        raise BrowserPackagesRefused(
            "The selected Node or pnpm executable is unavailable."
        )
    return await package_attempt(
        ("install", str(Path(pnpm).absolute()), str(Path(node).absolute().parent)),
        allowance,
    )


def accepted(deadline: str) -> bool:
    with sys.stdin.buffer as admission:
        record = admission.readline(ADMISSION_BYTES)
    parts = record.decode("ascii", "replace").split()
    if len(parts) != 2 or not record.endswith(b"\n") or not deadline.isdecimal():
        return False
    invocation, cgroup = parts
    return (
        bool(INVOCATION.fullmatch(invocation))
        and os.environ.get("INVOCATION_ID") == invocation
        and bool(SERVICE_PATH.fullmatch(cgroup))
        and Path("/proc/self/cgroup").read_text(encoding="ascii") == f"0::{cgroup}\n"
        and time.monotonic_ns() < int(deadline)
    )


async def run_child(command: Sequence[str], environment: dict[str, str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL, env=environment, cwd=ROOT
    )
    return await process.wait()


async def worker(arguments: Sequence[str]) -> int:
    if len(arguments) != 4 or os.geteuid() != 0:
        return 78
    deadline, operation, first, second = arguments
    if operation == "witness":
        witness = ROOT / "tests/architecture/browser_system_packages_witness.py"
        administrative = Path(first)
        if (
            not administrative.is_absolute()
            or not re.fullmatch(r"kora-browser-dpkg-[a-z0-9_]{8}", administrative.name)
            or second not in {"hold", "configure"}
            or not accepted(deadline)
        ):
            return 78
        return await run_child((PYTHON, "-I", str(witness), first, second), {})
    if operation != "install" or not all(
        Path(value).is_absolute() for value in (first, second)
    ):
        return 78
    environment = {"PATH": f"{Path(first).parent}:{second}:/usr/bin:/bin"}
    if not accepted(deadline):
        return 78
    recovery = await run_child((DPKG, "--configure", "-a"), environment)
    if recovery:
        return recovery
    return await run_child(
        (
            first,
            "--dir",
            "web",
            "exec",
            "playwright",
            "install-deps",
            "chromium-headless-shell",
        ),
        environment,
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(worker(sys.argv[1:])))
