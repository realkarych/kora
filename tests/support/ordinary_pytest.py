import signal
import sys

import pytest


def main(arguments: list[str]) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    return int(pytest.main(arguments))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
