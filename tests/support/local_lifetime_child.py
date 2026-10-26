import json
import os
import signal
import socket
import sys


def main() -> int:
    mode, endpoint = sys.argv[1:]
    if mode == "inspect":
        descriptors: list[int] = []
        for descriptor in range(3, 256):
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            descriptors.append(descriptor)
        with open(endpoint, "w", encoding="utf-8") as result:
            json.dump(
                {
                    "pid": os.getpid(),
                    "group": os.getpgrp(),
                    "session": os.getsid(0),
                    "descriptors": descriptors,
                    "environment": dict(os.environ),
                },
                result,
            )
        return 0
    if mode == "descendant":
        reading, writing = os.pipe()
        child = os.fork()
        if child:
            os.close(writing)
            ready = os.read(reading, 1)
            os.close(reading)
            return 0 if ready == b"r" else 1
        os.close(reading)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(writing, b"r")
        os.close(writing)
    elif mode == "resistant":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        return 2
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect(endpoint)
        peer.sendall(str(os.getpid()).encode("ascii") + b"\n")
        return 0 if peer.recv(1) == b"x" else 3


if __name__ == "__main__":
    raise SystemExit(main())
