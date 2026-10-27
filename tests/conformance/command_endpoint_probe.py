import socket
import sys


def run(address: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.connect(address)
        if endpoint.recv(1):
            raise ValueError("unexpected endpoint input")


if __name__ == "__main__":
    run(sys.argv[1])
