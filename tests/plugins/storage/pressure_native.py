import ctypes
import errno
import os
import select
import selectors
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from tests.plugins.storage.pressure_models import PressureFailure, ProcessToken


def process_token(pid: int, *, include_exited: bool = False) -> ProcessToken | None:
    if sys.platform == "linux":
        try:
            status = Path(f"/proc/{pid}/stat").read_bytes()
        except FileNotFoundError:
            return None
        fields = status.rsplit(b") ", 1)[1].split()
        if fields[0] == b"Z" and not include_exited:
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return ProcessToken(pid=pid, identity=f"linux:{boot}:{int(fields[19])}")
    if sys.platform != "darwin":
        raise PressureFailure("platform_unsupported")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    info = library.proc_pidinfo
    info.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    info.restype = ctypes.c_int
    state = ctypes.create_string_buffer(56)
    ctypes.set_errno(0)
    count = info(pid, 17, int(include_exited), state, len(state))
    if count == 0 and ctypes.get_errno() == errno.ESRCH:
        return None
    if count != 56:
        raise PressureFailure("process_identity_unavailable")
    unique = struct.unpack_from("@Q", state.raw, 16)[0]
    version = struct.unpack_from("@i", state.raw, 32)[0]
    return ProcessToken(pid=pid, identity=f"darwin:{unique}:{version}")


def wait_closed(token: ProcessToken, seconds: float) -> None:
    if process_token(token.pid) != token:
        return
    if sys.platform == "linux":
        try:
            descriptor = os.pidfd_open(token.pid)
        except ProcessLookupError:
            return
        try:
            if process_token(token.pid) != token:
                return
            readable, _, _ = select.select([descriptor], [], [], seconds)
            if not readable:
                raise PressureFailure("child_closure_unresolved")
        finally:
            os.close(descriptor)
    else:
        queue = select.kqueue()
        try:
            event = select.kevent(
                token.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                events = queue.control([event], 1, seconds)
            except ProcessLookupError:
                return
            if not events and process_token(token.pid) == token:
                raise PressureFailure("child_closure_unresolved")
        finally:
            queue.close()
    if process_token(token.pid) == token:
        raise PressureFailure("child_closure_unresolved")


def collect_process(
    process: subprocess.Popen[bytes],
    seconds: float,
) -> tuple[bytes, bytes, int]:
    output = bytearray()
    error_output = bytearray()
    if process.stdout is None or process.stderr is None:
        raise PressureFailure("command_pipe_missing")
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, error_output)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PressureFailure("command_deadline")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target: object = key.data
                    if not isinstance(target, bytearray):
                        raise PressureFailure("command_pipe_invalid")
                    if len(output) + len(error_output) + len(chunk) > 1048576:
                        raise PressureFailure("command_output_limit")
                    target.extend(chunk)
        status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return bytes(output), bytes(error_output), status
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def command(
    arguments: tuple[str, ...],
    seconds: float = 30,
    *,
    on_closed: Callable[[], None] | None = None,
    on_started: Callable[[ProcessToken], None] | None = None,
    on_finished: Callable[[int], None] | None = None,
) -> bytes:
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        if on_closed is not None:
            on_closed()
        raise PressureFailure("command_start_failed") from None
    try:
        if on_started is not None:
            token = process_token(process.pid, include_exited=True)
            if token is None:
                raise PressureFailure("command_identity_unavailable")
            on_started(token)
        output, _, status = collect_process(process, seconds)
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    if status < 0:
        raise PressureFailure("command_terminated")
    if on_finished is not None:
        on_finished(status)
    if on_closed is not None:
        on_closed()
    if status != 0:
        raise PressureFailure("command_failed")
    return output
