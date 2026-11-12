import os
import signal
import sys
from contextlib import closing

from kora.plugins.vcs.fake import store


def main() -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    _, arguments = store.parsed_arguments(sys.argv[2:])
    stages = (
        store.importation(arguments.text("input"), arguments.identifier("base"))
        if sys.argv[2] == "import"
        else store.publication(
            arguments.text("repository"),
            arguments.branch(),
            arguments.identifier("base"),
            arguments.identifier("prepared"),
        )
    )
    with closing(stages):
        for _ in stages:
            descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(descriptor, b"ready")
            finally:
                os.close(descriptor)
            signal.sigwait({signal.SIGTERM})
            raise SystemExit(143)


if __name__ == "__main__":
    main()
