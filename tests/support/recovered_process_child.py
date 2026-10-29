import os
import select
import signal
import socket
import subprocess
import sys


def held_control(peer: socket.socket) -> bytes:
    readable, _, _ = select.select((peer, sys.stdin.buffer), (), ())
    return peer.recv(1) if peer in readable else sys.stdin.buffer.read(1)


def run_held(peer: socket.socket, address: str, mode: str, role: str) -> None:
    if role == "member":
        peer.sendall(str(os.getpid()).encode())
        held_control(peer)
        return
    if role == "leader":
        if mode == "held_family":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        peer.sendall(str(os.getpid()).encode())
        if mode != "held_family" or held_control(peer) != b"s":
            if mode != "held_family":
                held_control(peer)
            return
        child = subprocess.Popen(
            [sys.executable, "-I", __file__, address, mode, "member"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            if held_control(peer) == b"r":
                child.wait()
                peer.sendall(b"reaped")
                held_control(peer)
        finally:
            if child.stdin is not None:
                child.stdin.close()
            child.wait()
        return
    peer.sendall(b"reaper")
    if peer.recv(1) != b"s":
        return
    child = subprocess.Popen(
        [sys.executable, "-I", __file__, address, mode, "leader"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        process_group=0,
        close_fds=True,
    )
    try:
        if peer.recv(1) == b"r":
            result = child.wait()
            peer.sendall(str(result).encode())
            peer.recv(1)
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait()


def run(address: str, mode: str, role: str) -> None:
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.connect(address)
    if mode in ("held", "held_family"):
        try:
            run_held(peer, address, mode, role)
        finally:
            peer.close()
        return
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
