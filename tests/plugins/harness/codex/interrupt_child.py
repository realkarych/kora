import os
import socket
from pathlib import Path


def main() -> None:
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect("/var/lib/kora-hook-probe/control/interrupt.sock")
        channel.sendall(f"{os.getpid()}\n".encode("ascii"))
        if channel.recv(1) == b"x":
            Path("kora-probe.marker").write_bytes(b"released\n")


if __name__ == "__main__":
    main()
