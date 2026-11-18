import asyncio
import ctypes
import os
import resource
import signal
import socket
import stat
import sys
import time
from pathlib import Path
from types import FrameType as SignalFrame
from typing import Final

from pydantic import ValidationError

from kora.ir.vcs_ref_json import JsonRefusal, decode_strict_json
from kora.plugins.runtime.local.channel import (
    PRE_ATTACH_BYTES,
    FrameType,
    LaunchCapability,
    LaunchExit,
    LaunchFailure,
    LaunchSignal,
    LaunchSpecEnvelope,
    LaunchSpecRequest,
    LaunchStarted,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import (
    MAX_RECORD_BYTES,
    TYPED_HEADER,
    BoundedChannel,
    ChannelError,
)
from kora.runner.filesystem import DirectoryAnchor, FilesystemError

PR_SET_DUMPABLE: Final[int] = 4


def capability_at(directory: DirectoryAnchor) -> LaunchCapability:
    descriptor = os.open(
        "capability",
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        dir_fd=directory.descriptor,
    )
    try:
        entry = os.fstat(descriptor)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) != 0o400
            or entry.st_nlink != 1
            or entry.st_size > PRE_ATTACH_BYTES
        ):
            raise ValueError("invalid capability file")
        payload = os.read(descriptor, PRE_ATTACH_BYTES + 1)
        decoded = decode_strict_json(payload, PRE_ATTACH_BYTES, 1)
        if isinstance(decoded, JsonRefusal):
            raise ValueError("invalid capability file")
        return LaunchCapability.model_validate(decoded)
    finally:
        os.close(descriptor)


def harden() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform == "linux":
        library = ctypes.CDLL(None, use_errno=True)
        if library.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            failure = ctypes.get_errno()
            raise OSError(failure, os.strerror(failure))


async def receive(
    endpoint: socket.socket, capability: LaunchCapability
) -> LaunchSpecEnvelope:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    endpoint.setblocking(False)
    async with asyncio.timeout(5):
        await asyncio.get_running_loop().sock_connect(endpoint, capability.socket_path)
    channel = BoundedChannel(endpoint, clock)
    request = LaunchSpecRequest(
        op="launch_spec",
        request_id=capability.launch_id,
        launch_id=capability.launch_id,
        capability=capability.capability,
    )
    await channel.write_typed_frame(
        FrameType.JSON, request.model_dump_json().encode(), deadline
    )
    _, payload = await channel.read_typed_frame(
        deadline, {FrameType.JSON.value: MAX_RECORD_BYTES}
    )
    decoded = decode_strict_json(payload, MAX_RECORD_BYTES, 4)
    if isinstance(decoded, JsonRefusal):
        raise ValueError("invalid launch response")
    spec = LaunchSpecEnvelope.model_validate(decoded)
    if spec.request_id != request.request_id:
        raise ValueError("invalid launch response")
    return spec


def report(
    endpoint: socket.socket,
    record: LaunchFailure | LaunchStarted | LaunchExit | LaunchSignal,
) -> None:
    payload = record.model_dump_json().encode()
    endpoint.sendall(TYPED_HEADER.pack(len(payload), FrameType.JSON) + payload)


def kill_group(signum: int, frame: SignalFrame | None) -> None:
    os.killpg(os.getpgrp(), signal.SIGKILL)


def forward(signum: int, frame: SignalFrame | None) -> None:
    signal.signal(signum, signal.SIG_IGN)
    try:
        os.killpg(os.getpgrp(), signum)
    finally:
        signal.signal(signum, forward)
    if signal.getitimer(signal.ITIMER_REAL)[0] == 0:
        signal.setitimer(signal.ITIMER_REAL, 3.0)


def spawn(endpoint: socket.socket, spec: LaunchSpecEnvelope, argv: list[str]) -> int:
    if not Path(spec.executable).is_absolute() or not Path(spec.cwd).is_absolute():
        raise ValueError("invalid launch paths")
    environment = {item.name: item.value for item in spec.environment}
    if len(environment) != len(spec.environment) or environment.get("PWD") != spec.cwd:
        raise ValueError("invalid launch environment")
    descriptor = endpoint.fileno()
    os.set_inheritable(descriptor, False)
    maximum = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if maximum == resource.RLIM_INFINITY:
        maximum = os.sysconf("SC_OPEN_MAX")
    os.closerange(3, descriptor)
    os.closerange(descriptor + 1, maximum)
    os.setsid()
    os.chdir(spec.cwd)
    forwarded = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, forwarded)
    try:
        signal.signal(signal.SIGALRM, kill_group)
        for signum in forwarded:
            signal.signal(signum, forward)
        child = os.posix_spawn(
            spec.executable,
            [spec.executable, *argv],
            environment,
            setsigmask=(),
            setsigdef=(*forwarded, signal.SIGALRM, signal.SIGPIPE, signal.SIGXFSZ),
        )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    return child


def supervise(endpoint: socket.socket, child: int) -> int:
    try:
        report(endpoint, LaunchStarted(op="started", pgid=os.getpid()))
    except OSError:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        raise
    _, status = os.waitpid(child, 0)
    signal.setitimer(signal.ITIMER_REAL, 0)
    result = os.waitstatus_to_exitcode(status)
    if result < 0:
        report(endpoint, LaunchSignal(op="signal", signal=-result))

    else:
        report(endpoint, LaunchExit(op="exit", exit_code=result))
    deadline = time.monotonic() + 5
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            endpoint.settimeout(remaining)
            if not endpoint.recv(1):
                break
    except TimeoutError:
        pass
    if result < 0:
        if -result != signal.SIGKILL:
            signal.signal(-result, signal.SIG_DFL)
        os.kill(os.getpid(), -result)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv if argv is None else argv
    if arguments[1:] == ["-v"]:
        return 0
    if not arguments:
        return 70
    try:
        harden()
        directory = DirectoryAnchor.open_path(Path(arguments[0]).parent)
        try:
            capability = capability_at(directory)
        finally:
            directory.close()
    except (OSError, ValueError, FilesystemError, ValidationError):
        return 70
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        spec = asyncio.run(receive(endpoint, capability))
        endpoint.setblocking(True)
        endpoint.settimeout(5)
        child = spawn(endpoint, spec, arguments[1:])
        del spec
    except (OSError, ValueError, ValidationError, ChannelError):
        try:
            endpoint.settimeout(5)
            report(endpoint, LaunchFailure(op="launch_failed"))
        except OSError:
            endpoint.close()
            return 72
        endpoint.close()
        return 72
    try:
        return supervise(endpoint, child)
    except OSError:
        return 72
    finally:
        endpoint.close()


if __name__ == "__main__":
    raise SystemExit(main())
