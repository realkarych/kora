import os
import select
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.ci.mutation_models import MAX_OUTPUT, MAX_WORK


@dataclass
class Stop:
    requested: bool = False

    def receive(self, number: int, frame: object) -> None:
        self.requested = True


@dataclass(frozen=True)
class Execution:
    code: int
    output: bytes
    reason: str


class ClosureFailure(RuntimeError):
    pass


def environment(root: Path) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(root),
        "TMPDIR": str(root),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_NO_PROGRESS": "1",
        "UV_NO_CACHE": "0",
        "KORA_TEST_DATABASES": "sqlite",
        "TESTCONTAINERS_RYUK_DISABLED": "true",
    }


def close_group(process: subprocess.Popen[bytes]) -> None:
    until = time.monotonic() + 2
    with selectors.DefaultSelector() as selector:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            if not exited(process):
                raise
        while not exited(process) and time.monotonic() < until:
            selector.select(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if not exited(process):
            raise
    process.wait(timeout=2)
    until = time.monotonic() + 1
    with selectors.DefaultSelector() as selector:
        while group_present(process.pid) and time.monotonic() < until:
            selector.select(0.01)
    if group_present(process.pid):
        raise ClosureFailure("unclosed audit process group")


def terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        close_group(process)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ClosureFailure("unconfirmed audit process group") from error


def group_present(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def exited(process: subprocess.Popen[bytes]) -> bool:
    if sys.platform == "linux":
        return (
            os.waitid(
                os.P_PID,
                process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            is not None
        )
    if sys.platform == "darwin":
        queue = select.kqueue()
        try:
            event = select.kevent(
                process.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            return bool(queue.control([event], 1, 0))
        finally:
            queue.close()
    raise ValueError("unsupported mutation audit platform")


def workspace_bytes(root: Path) -> int:
    total = 0
    for directory, children, files in os.walk(root):
        if Path(directory) == root and ".venv" in children:
            children.remove(".venv")
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except FileNotFoundError:
                continue
    return total


def execute(
    arguments: list[str],
    root: Path,
    deadline: float,
    stop: Stop,
    *,
    variables: dict[str, str] | None = None,
    limit: int = MAX_OUTPUT,
    workspace: Path | None = None,
    workspace_limit: int = MAX_WORK,
) -> Execution:
    if stop.requested or time.monotonic() >= deadline:
        return Execution(-1, b"", "interrupted" if stop.requested else "timeout")
    process = subprocess.Popen(
        arguments,
        cwd=root,
        env=variables or environment(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output = bytearray()
    reason = "completed"
    try:
        if process.stdout is None:
            raise RuntimeError("missing child output")
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or not exited(process):
                if (
                    workspace is not None
                    and workspace_bytes(workspace) > workspace_limit
                ):
                    reason = "workspace_limit"
                    break
                remaining = deadline - time.monotonic()
                if stop.requested or remaining <= 0:
                    reason = "interrupted" if stop.requested else "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                    elif len(output) + len(chunk) > limit:
                        reason = "output_limit"
                        break
                    else:
                        output.extend(chunk)
                if reason != "completed":
                    break
    finally:
        try:
            terminate(process)
        finally:
            if process.stdout is not None:
                process.stdout.close()
    return Execution(process.returncode, bytes(output), reason)
