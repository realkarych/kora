import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import FrameType


def terminate(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(0)


def serve(directory: Path) -> None:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, terminate)
    signal.alarm(900)
    worker: subprocess.Popen[bytes] | None = None
    path = directory / "control.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        try:
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
                        if worker is not None:
                            worker.kill()
                            worker.wait(timeout=10)
                            worker = None
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
                    previous = signal.pthread_sigmask(
                        signal.SIG_BLOCK, {signal.SIGTERM}
                    )
                    try:
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
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
                    endpoint.sendall(f"{worker.pid}\n".encode())
        finally:
            if worker is not None:
                worker.kill()
                worker.wait(timeout=10)
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
