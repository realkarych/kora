import os
import signal
import socket
import subprocess
import sys


def run(address: str, mode: str, role: str) -> None:
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.connect(address)
    if role in ("leader", "member"):
        if mode == "resistant":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if role == "leader" and mode == "family":
            subprocess.Popen(
                [sys.executable, "-I", __file__, address, mode, "member"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        peer.sendall(str(os.getpid()).encode())
        peer.recv(1)
    else:
        peer.sendall(b"reaper")
        child = subprocess.Popen(
            [sys.executable, "-I", __file__, address, mode, "leader"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            process_group=0,
            close_fds=True,
        )
        result = child.wait()
        peer.sendall(str(result).encode())
        peer.recv(1)
    peer.close()


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3])
