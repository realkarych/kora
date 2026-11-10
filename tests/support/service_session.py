import os
import signal
import socket
import subprocess
import sys
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import FrameType

LISTENING_MARKER = "control.listening"


def terminate(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(0)


@contextmanager
def defer_termination() -> Generator[None]:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def serve(directory: Path) -> None:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, terminate)
    signal.alarm(900)
    worker: subprocess.Popen[bytes] | None = None
    path = directory / "control.sock"
    listening = directory / LISTENING_MARKER

    def close_worker() -> None:
        nonlocal worker
        if worker is not None:
            worker.kill()
            worker.wait(timeout=10)
            worker = None

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        owned = ExitStack()
        try:
            with defer_termination():
                if os.path.lexists(listening):
                    raise FileExistsError("fixture listening marker already exists")
                server.bind(str(path))
                owned.callback(path.unlink, missing_ok=True)
                owned.callback(close_worker)
                server.listen(1)
                marker = os.open(listening, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                owned.callback(listening.unlink, missing_ok=True)
                os.close(marker)
            while True:
                endpoint, _ = server.accept()
                with endpoint:
                    endpoint.settimeout(10)
                    with endpoint.makefile("rb") as source:
                        message = source.readline(65537)
                    if len(message) > 65536 or not message.endswith(b"\n"):
                        raise ValueError("fixture message is not bounded")
                    arguments = message[:-1].decode().split("\0")
                    if arguments == ["close"]:
                        with defer_termination():
                            listening.unlink(missing_ok=True)
                            close_worker()
                            endpoint.sendall(b"closing\n")
                        return
                    if (
                        len(arguments) not in (3, 4)
                        or len(arguments) == 4
                        and arguments[3] != "linger-false"
                        or worker is not None
                        or not all(Path(value).is_absolute() for value in arguments[:3])
                    ):
                        raise ValueError("fixture request is not supported")
                    with defer_termination():
                        with (directory / "worker.log").open("wb") as output:
                            worker = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-m",
                                    "tests.support.service_worker",
                                    *arguments,
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=output,
                                stderr=subprocess.STDOUT,
                            )
                    endpoint.sendall(f"{worker.pid}\n".encode())
        finally:
            with defer_termination():
                owned.close()


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
