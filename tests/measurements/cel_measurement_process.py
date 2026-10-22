import os
import selectors
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessResult:
    pid: int
    code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    overflow: bool


def run_process(
    argv: Sequence[str],
    *,
    deadline_seconds: float = 10.0,
    output_limit: int = 262144,
) -> ProcessResult:
    if deadline_seconds <= 0 or output_limit < 1:
        raise ValueError("measurement process limits must be positive")
    deadline = time.monotonic() + deadline_seconds
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = False
    overflow = False
    environment = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
    with subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    ) as child:
        assert child.stdout is not None
        assert child.stderr is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, "stdout")
                selector.register(child.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map() and not overflow:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(remaining):
                        name = str(key.data)
                        target = outputs[name]
                        chunk = os.read(
                            key.fd, min(65536, output_limit - len(target) + 1)
                        )
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = output_limit - len(target)
                        target.extend(chunk[:available])
                        if len(chunk) > available:
                            overflow = True
                            break
            if not timed_out and not overflow:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                else:
                    try:
                        child.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        timed_out = True
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
        return ProcessResult(
            child.pid,
            child.returncode,
            bytes(outputs["stdout"]),
            bytes(outputs["stderr"]),
            timed_out,
            overflow,
        )
