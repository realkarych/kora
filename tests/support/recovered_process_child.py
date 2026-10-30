import os
import select
import signal
import socket
import subprocess
import sys

from kora.runner.identity import IdentityState, NativeIdentityReader


def verify_collected_child(child: subprocess.Popen[bytes], mode: str) -> None:
    reader = NativeIdentityReader()
    assert reader.read(child.pid).state is IdentityState.ABSENT
    group = reader.group(child.pid)
    assert group.state is IdentityState.PRESENT
    if group.members:
        assert mode == "family"
        assert child.returncode == -signal.SIGKILL
    else:
        os.write(1, b"r")


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
        verify_collected_child(child, mode)


def run_legacy(peer: socket.socket, address: str, mode: str, role: str) -> None:
    if role == "member":
        peer.sendall(str(os.getpid()).encode())
        peer.recv(1)
        return
    if role == "leader":
        if mode == "resistant":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        peer.sendall(str(os.getpid()).encode())
        if mode != "family" or held_control(peer) != b"s":
            if mode != "family":
                held_control(peer)
            return
        child = subprocess.Popen(
            [sys.executable, "-I", __file__, address, mode, "member"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            held_control(peer)
        finally:
            child.wait()
        return
    peer.sendall(b"reaper")
    if held_control(peer) != b"s":
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
        result = child.wait()
        peer.sendall(str(result).encode())
        held_control(peer)
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait()
        verify_collected_child(child, mode)


def run(address: str, mode: str, role: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        try:
            peer.connect(address)
        except (ConnectionRefusedError, FileNotFoundError):
            return
        try:
            if mode in ("held", "held_family"):
                run_held(peer, address, mode, role)
            else:
                run_legacy(peer, address, mode, role)
        except (BrokenPipeError, ConnectionResetError):
            return


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3])
