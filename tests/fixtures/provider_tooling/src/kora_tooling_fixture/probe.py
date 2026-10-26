import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from kora.__main__ import InstalledPluginDiscovery
from kora.cli import CLI_COMPONENT
from kora.cli.plugins import ListingError, PluginListing
from kora.observability import LoggingSettings, configure_process
from kora.protocols.lifetime import ProcessRegistration
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.runner.processes import GatedProcess
from kora_tooling_fixture.clock import ProbeClock
from kora_tooling_fixture.control import configuration


class Peer(BaseModel):
    pid: int


class ClosureEvidence(BaseModel):
    state: Literal["closure_unknown"]
    process: ProcessRegistration


def descriptors() -> int:
    path = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
    return len(os.listdir(path))


async def peer(listener: socket.socket) -> tuple[socket.socket, int]:
    endpoint, _ = await asyncio.get_running_loop().sock_accept(listener)
    raw = b""
    while not raw.endswith(b"\n"):
        received = await asyncio.get_running_loop().sock_recv(endpoint, 1024 - len(raw))
        if not received or len(raw) + len(received) > 1024:
            endpoint.close()
            raise ValueError("incomplete probe identity")
        raw += received
    return endpoint, Peer.model_validate_json(raw).pid


async def discover_under_handled_error(
    invocation: InstalledPluginDiscovery,
) -> PluginListing:
    try:
        raise ValueError("unrelated handled exception")
    except ValueError:
        return await invocation.discover()


async def exercise(case: str) -> dict[str, object]:
    clock = ProbeClock()
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "listing-probe", clock.now
    )
    invocation = InstalledPluginDiscovery(diagnostics, clock)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(configuration().socket)
    listener.listen(2)
    listener.setblocking(False)
    connections: list[socket.socket] = []
    baseline = descriptors()
    operation = asyncio.create_task(
        discover_under_handled_error(invocation)
        if case == "retained"
        else invocation.discover()
    )
    try:
        async with asyncio.timeout(12):
            endpoint, pid = await peer(listener)
            connections.append(endpoint)
            process = invocation.process
            if process is None or process.registration is None:
                raise ValueError("missing owned process identity")
            started = clock.monotonic_ns()
            descendant_pid: int | None = None
            if case == "crash":
                endpoint.sendall(b"x")
            elif case == "retained":
                endpoint.sendall(b"r")
            elif case == "descendant":
                endpoint.sendall(b"d")
                descendant, descendant_pid = await peer(listener)
                connections.append(descendant)
            elif case == "timeout":
                await clock.advance_to_timer(started + 30_000_000_000)
                started = clock.monotonic_ns()
            else:
                operation.cancel()
            if case not in {"crash", "retained"}:
                if case == "repeat_cancel":
                    while started + 5_000_000_000 not in clock.waiters.values():
                        clock.changed.clear()
                        if started + 5_000_000_000 not in clock.waiters.values():
                            await clock.changed.wait()
                    operation.cancel()
                await clock.advance_to_timer(
                    started + 5_000_000_000,
                    current=started + 14_000_000_000 if case == "unknown" else None,
                )
                if configuration().resistant or case == "descendant":
                    await clock.advance_to_timer(started + 7_000_000_000)
            outcome = "completed"
            cleanup_reason: str | None = None
            retained_content: str | None = None
            try:
                await operation
            except asyncio.CancelledError as error:
                outcome = "cancelled"
                if isinstance(error.__cause__, ListingError):
                    if case == "unknown":
                        cleanup_reason, retained_content = await recover_unknown(
                            error.__cause__, process, clock, endpoint
                        )
                    else:
                        cleanup_reason, retained_content = remove_retained(
                            error.__cause__
                        )
            except ListingError as error:
                outcome = error.reason.partition(":")[0]
                if isinstance(error.__cause__, ListingError):
                    cleanup_reason, retained_content = remove_retained(error.__cause__)
                elif outcome == "state_retained":
                    cleanup_reason, retained_content = remove_retained(error)
            group = NativeIdentityReader().group(process.registration.process_group_id)
            return {
                "outcome": outcome,
                "cleanup_reason": cleanup_reason,
                "retained_content": retained_content,
                "closed": process.closed,
                "reaped": process.returncode is not None,
                "pid_absent": NativeIdentityReader().read(pid).state
                is IdentityState.ABSENT,
                "descendant_absent": descendant_pid is None
                or NativeIdentityReader().read(descendant_pid).state
                is IdentityState.ABSENT,
                "group_empty": group.state is IdentityState.PRESENT
                and not group.members,
                "descriptors_before": baseline,
                "descriptors_after": descriptors() - len(connections),
                "remaining_tasks": len(asyncio.all_tasks()),
            }
    finally:
        for endpoint in connections:
            endpoint.close()
        if not operation.done():
            operation.cancel()
        owner = invocation.process
        if owner is not None and not owner.closed:
            clock.use_system_time()
            owner.signal(signal.SIGKILL, clock.monotonic_ns() + 5_000_000_000)
        await asyncio.gather(operation, return_exceptions=True)
        if owner is not None and not owner.closed:
            deadline = clock.monotonic_ns() + 5_000_000_000
            await owner.settle(deadline)
            await owner.drain(deadline)
            owner.release(deadline)
        listener.close()
        Path(configuration().socket).unlink()
        diagnostics.shutdown()


def remove_retained(error: ListingError) -> tuple[str, str]:
    reason, _, raw_path = error.reason.partition(":")
    root = Path(raw_path)
    if (
        reason != "state_retained"
        or root.parent != Path(configuration().socket).parent.parent.resolve()
    ):
        raise ValueError("unexpected retained path")
    content = (root / "cwd" / "provider-owned.txt").read_text()
    (root / "cwd" / "provider-owned.txt").unlink()
    (root / "cwd").rmdir()
    root.rmdir()
    return reason, content


async def recover_unknown(
    error: ListingError, owner: GatedProcess, clock: ProbeClock, endpoint: socket.socket
) -> tuple[str, str]:
    reason, _, raw_path = error.reason.partition(":")
    root = Path(raw_path)
    if (
        reason != "closure_unknown"
        or root.parent != Path(configuration().socket).parent.parent.resolve()
    ):
        raise ValueError("unexpected uncertainty evidence path")
    evidence = ClosureEvidence.model_validate_json((root / "closure.json").read_bytes())
    if evidence.process != owner.registration or owner.closed:
        raise ValueError("uncertainty lost process ownership")
    if (root / "closure.json").stat().st_mode & 0o777 != 0o600:
        raise ValueError("uncertainty evidence is not private")
    clock.use_system_time()
    endpoint.sendall(b"r")
    deadline = clock.monotonic_ns() + 5_000_000_000
    await owner.settle(deadline)
    await owner.drain(deadline)
    owner.release(deadline)
    for name in ("closure.json", "r", "e"):
        (root / name).unlink()
    (root / "cwd").rmdir()
    root.rmdir()
    return reason, evidence.state


def main() -> None:
    result = asyncio.run(exercise(sys.argv[1]))
    os.write(1, json.dumps(result, sort_keys=True).encode())


if __name__ == "__main__":
    main()
