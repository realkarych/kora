import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from types import FrameType

from tests.cel_eval.children import CHILD_TIMEOUT_SECONDS, PRELUDE

READY: bytes = b"KORA_CEL_SINK_READY\n"
ACK: bytes = b"\x01"
READY_LIMIT: int = 32
EXIT_SECONDS: float = 1.0
OUTPUT_LIMIT: int = 262144


@dataclass(frozen=True)
class SinkProcessResult:
    pid: int
    code: int
    stdout: bytes
    stderr: bytes
    readiness: bytes
    total_seconds: float
    post_work_seconds: float | None
    timed_out: bool
    overflow: bool
    protocol_error: bool


def begin_cleanup() -> None:
    if os.write(1, READY) != len(READY) or os.read(0, 1) != ACK:
        raise RuntimeError("sink measurement acknowledgement is invalid")


def run_sink_child(
    code: str,
    path: Sequence[str],
    *arguments: str,
    deadline_seconds: float = CHILD_TIMEOUT_SECONDS,
    exit_seconds: float = EXIT_SECONDS,
    output_limit: int = OUTPUT_LIMIT,
) -> SinkProcessResult:
    if deadline_seconds <= 0 or exit_seconds <= 0 or output_limit < READY_LIMIT:
        raise ValueError("sink measurement process limits are invalid")
    started = time.monotonic()
    deadline = started + deadline_seconds
    cleanup_started: float | None = None
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    received = {"stdout": 0, "stderr": 0}
    readiness = bytearray()
    timed_out = False
    overflow = False
    protocol_error = False
    interrupted = False

    def defer_interrupt(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = True

    previous_handler = signal.signal(signal.SIGINT, defer_interrupt)
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                PRELUDE + code,
                json.dumps(list(path)),
                *arguments,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        ) as child:
            assert child.stdin is not None
            assert child.stdout is not None
            assert child.stderr is not None
            completed = False
            try:
                signal.signal(signal.SIGINT, previous_handler)
                if interrupted:
                    signal.raise_signal(signal.SIGINT)
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ, "stdout")
                    selector.register(child.stderr, selectors.EVENT_READ, "stderr")
                    while selector.get_map() and not overflow and not protocol_error:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        for key, _ in selector.select(remaining):
                            name = str(key.data)
                            limit = min(65536, output_limit - received[name] + 1)
                            if name == "stdout" and cleanup_started is None:
                                limit = min(limit, READY_LIMIT - len(readiness) + 1)
                            chunk = os.read(key.fd, limit)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                if name == "stdout" and cleanup_started is None:
                                    protocol_error = True
                                    break
                                continue
                            available = output_limit - received[name]
                            received[name] += len(chunk)
                            kept = chunk[:available]
                            if name == "stdout" and cleanup_started is None:
                                readiness.extend(kept)
                                if not READY.startswith(readiness):
                                    protocol_error = True
                                    break
                                if len(readiness) == len(READY):
                                    observed = time.monotonic()
                                    if observed >= deadline:
                                        timed_out = True
                                        break
                                    cleanup_started = observed
                                    deadline = min(deadline, observed + exit_seconds)
                                    try:
                                        if os.write(child.stdin.fileno(), ACK) != 1:
                                            protocol_error = True
                                    except BrokenPipeError:
                                        protocol_error = True
                                    child.stdin.close()
                            else:
                                outputs[name].extend(kept)
                            if len(chunk) > available:
                                overflow = True
                                break
                        if timed_out:
                            break
                if not timed_out and not overflow and not protocol_error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                    else:
                        try:
                            child.wait(timeout=remaining)
                        except subprocess.TimeoutExpired:
                            timed_out = True
                        else:
                            completed = True
            finally:
                try:
                    if not completed and child.poll() is None:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            if child.poll() is None:
                                raise
                finally:
                    try:
                        child.wait()
                    finally:
                        try:
                            child.stdin.close()
                        finally:
                            try:
                                child.stdout.close()
                            finally:
                                child.stderr.close()
            ended = time.monotonic()
            timed_out = timed_out or ended > deadline
            return SinkProcessResult(
                child.pid,
                child.returncode,
                bytes(outputs["stdout"]),
                bytes(outputs["stderr"]),
                bytes(readiness),
                ended - started,
                None if cleanup_started is None else ended - cleanup_started,
                timed_out,
                overflow,
                protocol_error,
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)
