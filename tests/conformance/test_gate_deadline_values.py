import asyncio
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from kora.protocols.lifetime import ProcessDomain
from kora.protocols.time import SystemClock
from kora.runner.gate import (
    GATE_DEADLINE_BYTES,
    MAX_GATE_DEADLINE_NS,
    decode_deadline,
    encode_deadline,
)
from kora.runner.processes import ExitAnchor, GatedProcess, start_gated
from tests.conformance.test_local_process_lifetime import launch


class OffsetSystemClock(SystemClock):
    def __init__(self, offset: int) -> None:
        self.offset = offset

    def monotonic_ns(self) -> int:
        return super().monotonic_ns() + self.offset


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["duration", "origin"])
async def test_gate_maps_accepted_large_instants_before_private_header_validation(
    tmp_path: Path, mode: str
) -> None:
    largest_duration = int(sys.float_info.max) * 1_000_000_000
    clock = OffsetSystemClock(2 * largest_duration if mode == "origin" else 0)
    deadline = clock.monotonic_ns() + (
        largest_duration if mode == "duration" else 5_000_000_000
    )
    baseline = len(os.listdir("/dev/fd"))
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner: GatedProcess | None = None
    waiting: asyncio.Task[None] | None = None

    try:
        owner = start_gated(
            launch("open('executed', 'w').close()", ProcessDomain.INVOCATION),
            "d" * 32,
            directory,
            deadline,
            clock,
        )
        registration = await asyncio.wait_for(owner.prepare(), 5)
        assert not (tmp_path / "executed").exists()
        waiting = asyncio.create_task(owner.wait_exit(deadline))
        entered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(entered.set_result, None)
        await asyncio.wait_for(entered, 5)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not owner.closed
        owner.close_gate()
        completion = clock.monotonic_ns() + 5_000_000_000
        await asyncio.wait_for(owner.settle(completion), 5)
        await asyncio.wait_for(owner.drain(completion), 5)
        owner.release(completion)

        assert owner.closed
        assert owner.returncode is not None
        assert owner.observe().capture_eof
        assert owner.readiness() == ()
        assert not (tmp_path / "executed").exists()
        with pytest.raises(ChildProcessError):
            os.waitpid(registration.process.pid, os.WNOHANG)
    finally:
        os.close(directory)
        if waiting is not None:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        if owner is not None and not owner.closed:
            completion = clock.monotonic_ns() + 5_000_000_000
            owner.signal(signal.SIGKILL, completion)
            await asyncio.wait_for(owner.settle(completion), 5)
            await asyncio.wait_for(owner.drain(completion), 5)
            owner.release(completion)

    assert len(os.listdir("/dev/fd")) == baseline


@pytest.mark.parametrize("value", [1, 2**64, MAX_GATE_DEADLINE_NS])
def test_gate_deadline_codec_preserves_exact_native_value(value: int) -> None:
    encoded = encode_deadline(value)

    decoded = decode_deadline(encoded)

    assert len(encoded) == GATE_DEADLINE_BYTES == 132
    assert decoded == value


@pytest.mark.parametrize("value", [False, 0, -1, 1.5, MAX_GATE_DEADLINE_NS + 1])
def test_gate_deadline_encoder_refuses_invalid_native_values(value: object) -> None:
    with pytest.raises(ValueError, match="gate deadline"):
        encode_deadline(value)


@pytest.mark.parametrize(
    "header",
    [
        b"",
        bytes(GATE_DEADLINE_BYTES - 1),
        bytes(GATE_DEADLINE_BYTES + 1),
        bytes(GATE_DEADLINE_BYTES),
        (MAX_GATE_DEADLINE_NS + 1).to_bytes(GATE_DEADLINE_BYTES, "big"),
    ],
)
def test_gate_deadline_decoder_refuses_invalid_fixed_header(header: bytes) -> None:
    with pytest.raises(ValueError, match="gate deadline"):
        decode_deadline(header)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, -1, 1.5, 2 * MAX_GATE_DEADLINE_NS])
async def test_gate_deadline_preflight_refuses_before_descriptor_or_child_effect(
    tmp_path: Path, value: object
) -> None:
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    baseline = len(os.listdir("/dev/fd"))
    request = launch("open('executed', 'w').close()", ProcessDomain.INVOCATION)
    try:
        with pytest.raises(ValueError, match="deadline is"):
            start_gated(request, "e" * 32, directory, cast(int, value), SystemClock())

        assert len(os.listdir("/dev/fd")) == baseline
        assert not (tmp_path / "executed").exists()
    finally:
        os.close(directory)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "zero", "oversized"])
async def test_real_gate_rejects_deadline_header_before_execution_body(
    tmp_path: Path, failure: str
) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    header = (
        b"x"
        if failure == "incomplete"
        else bytes(GATE_DEADLINE_BYTES)
        if failure == "zero"
        else (MAX_GATE_DEADLINE_NS + 1).to_bytes(GATE_DEADLINE_BYTES, "big")
    )
    baseline = len(os.listdir("/dev/fd"))
    parent, child = socket.socketpair()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    process: subprocess.Popen[bytes] | None = None
    anchor: ExitAnchor | None = None
    try:
        parent.sendall(header)
        process = subprocess.Popen(
            [sys.executable, "-I", "-m", "kora.runner.gate"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(child.fileno(), directory),
            process_group=0,
            env={
                "KORA_GATE_CONTROL_FD": str(child.fileno()),
                "KORA_GATE_CWD_FD": str(directory),
            },
        )
        anchor = ExitAnchor(process.pid)
        child.close()
        if failure == "incomplete":
            parent.close()

        await anchor.wait(deadline, clock)
        pid, status = os.waitpid(process.pid, os.WNOHANG)
        assert pid == process.pid
        process.returncode = os.waitstatus_to_exitcode(status)

        assert process.returncode == 71
        assert not (tmp_path / "executed").exists()
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
    finally:
        parent.close()
        child.close()
        os.close(directory)
        try:
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.to_thread(process.wait, 5)
        finally:
            if anchor is not None:
                anchor.close()

    assert len(os.listdir("/dev/fd")) == baseline
