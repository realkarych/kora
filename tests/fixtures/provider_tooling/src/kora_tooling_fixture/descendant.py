import signal

from kora_tooling_fixture.control import connect


def main() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with connect() as endpoint:
        endpoint.recv(1)


if __name__ == "__main__":
    main()
