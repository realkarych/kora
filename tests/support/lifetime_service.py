import socket
import sys


def run(role: str, address: str, observation: str) -> None:
    if role == "client":
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.connect(address)
        peer.recv(1)
        peer.close()
        return
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(address)
    listener.listen(1)
    feedback = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    feedback.connect(observation)
    feedback.sendall(b"r")
    peer, _ = listener.accept()
    feedback.sendall(b"c")
    peer.recv(1)
    peer.close()
    feedback.sendall(b"e")
    feedback.recv(1)
    feedback.close()
    listener.close()


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3])
