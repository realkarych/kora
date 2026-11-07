import os
import signal
import sys
from contextlib import closing

from kora.plugins.vcs.fake import store


def main() -> None:
    signal.signal(signal.SIGTERM, store.terminate)
    _, arguments = store.parsed_arguments(sys.argv[2:])
    stages = store.publication(
        arguments.text("repository"),
        arguments.branch(),
        arguments.identifier("base"),
        arguments.identifier("prepared"),
    )
    with closing(stages):
        for _ in stages:
            descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(descriptor, b"ready")
            finally:
                os.close(descriptor)
            signal.pause()


if __name__ == "__main__":
    main()
