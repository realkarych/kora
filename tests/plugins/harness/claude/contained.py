import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

from kora.protocols.time import Clock

IMAGE = (
    "ubuntu:24.04@sha256:"
    "73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8"
)
COUNTERS = ("Ip:OutNoRoutes", "Ip6OutNoRoutes", "Udp:OutDatagrams", "Udp6OutDatagrams")


def processes_naming(marker: str) -> list[str]:
    proc = Path("/proc")
    entries = (
        [entry for entry in proc.iterdir() if entry.name.isdecimal()]
        if proc.is_dir()
        else []
    )
    current = os.getpid()
    found: list[str] = []
    if entries:
        for entry in entries:
            if int(entry.name) == current:
                continue
            try:
                command = " ".join(
                    entry.joinpath("cmdline")
                    .read_bytes()
                    .decode(errors="replace")
                    .split("\0")
                )
            except (FileNotFoundError, ProcessLookupError):
                continue
            directory = working_directory(entry)
            if marker in command or directory.startswith(marker):
                found.append(command)
    else:
        listed = subprocess.run(
            ["/bin/ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in listed.stdout.splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) == 2 and int(fields[0]) != current and marker in fields[1]:
                found.append(fields[1])
    return found


def working_directory(entry: Path) -> str:
    try:
        return os.readlink(entry / "cwd")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return ""


async def released(marker: str, clock: Clock, deadline_ns: int) -> list[str]:
    while True:
        remaining = processes_naming(marker)
        now = clock.monotonic_ns()
        if not remaining or now >= deadline_ns:
            return remaining
        await clock.sleep_until(min(deadline_ns, now + 50_000_000))


def network_counters() -> dict[str, int]:
    counters: dict[str, int] = {}
    lines = Path("/proc/net/snmp").read_text().splitlines()
    for header, values in zip(lines[::2], lines[1::2], strict=True):
        names = header.split()
        fields = values.split()
        assert names[0] == fields[0]
        for name, value in zip(names[1:], fields[1:], strict=True):
            key = names[0] + name
            if key in COUNTERS:
                counters[key] = int(value)
    for line in Path("/proc/net/snmp6").read_text().splitlines():
        name, value = line.split()
        if name in COUNTERS:
            counters[name] = int(value)
    return counters


def probe_outbound() -> None:
    for family, address in (
        (socket.AF_INET, ("192.0.2.1", 9)),
        (socket.AF_INET6, ("2001:db8::1", 9)),
    ):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(family, kind) as connection:
                connection.settimeout(5)
                try:
                    if kind == socket.SOCK_STREAM:
                        connection.connect(address)
                    else:
                        connection.sendto(b"probe", address)
                except TimeoutError:
                    pass
                except OSError:
                    continue
                raise AssertionError("the network namespace has an outbound route")


def container_command(
    argv: list[str],
    *,
    docker: str,
    name: str,
    writable: Path,
    environment: Mapping[str, str],
    cwd: Path,
) -> list[str]:
    roots = {
        Path(__file__).resolve().parents[4],
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.executable).resolve().parent,
    }
    mounts = sorted(
        root
        for root in roots
        if not any(root != other and root.is_relative_to(other) for other in roots)
    )
    command = [
        docker,
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs=/tmp:rw,exec,mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--workdir",
        str(cwd),
    ]
    for key, value in environment.items():
        command.extend(("-e", f"{key}={value}"))
    for root in mounts:
        command.extend(("-v", f"{root}:{root}:ro"))
    command.extend(("-v", f"{writable}:{writable}:rw", IMAGE, *argv))
    return command


def run_contained(
    argv: list[str],
    *,
    writable: Path,
    environment: Mapping[str, str],
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required for contained Claude cases"
    name = f"kora-claude-{uuid4().hex}"
    try:
        return subprocess.run(
            container_command(
                argv,
                docker=docker,
                name=name,
                writable=writable,
                environment=environment,
                cwd=cwd,
            ),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    finally:
        remove_container(docker, name)


def remove_container(docker: str, name: str) -> None:
    removed = subprocess.run(
        [docker, "rm", "-f", name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert removed.returncode == 0 or any(
        expected in removed.stderr
        for expected in ("No such container", "is already in progress")
    ), removed.stderr


def counter_mapping(value: object) -> dict[str, int]:
    assert isinstance(value, Mapping)
    fields = cast("Mapping[object, object]", value)
    counters: dict[str, int] = {}
    for name in COUNTERS:
        count = fields[name]
        assert type(count) is int
        counters[name] = count
    return counters


def assert_no_outbound(report: dict[str, object]) -> None:
    network = report["network"]
    if sys.platform != "linux":
        assert network is None
        return
    assert isinstance(network, Mapping)
    fields = cast("Mapping[object, object]", network)
    before = counter_mapping(fields["before"])
    after = counter_mapping(fields["after"])
    probed = counter_mapping(fields["probed"])
    assert after == before
    assert probed["Ip:OutNoRoutes"] > after["Ip:OutNoRoutes"]
    assert probed["Ip6OutNoRoutes"] > after["Ip6OutNoRoutes"]
