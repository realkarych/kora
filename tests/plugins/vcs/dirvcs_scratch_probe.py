import os
import signal
import sys

from kora.plugins.vcs.fake import store


def main() -> None:
    directory = os.open(sys.argv[1], store.DIRECTORY_FLAGS)
    signal.signal(signal.SIGTERM, store.terminate)
    try:
        scratch = store.scratch_file(directory)
        os.write(scratch.descriptor, b"incomplete")
        os.write(sys.stdout.fileno(), b"ready\n")
        signal.pause()
    finally:
        store.discard_all()
        os.close(directory)


if __name__ == "__main__":
    main()
