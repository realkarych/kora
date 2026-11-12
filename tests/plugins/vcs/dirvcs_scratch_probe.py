import os
import signal
import sys

from kora.plugins.vcs.fake import store


def main() -> None:
    directory = os.open(sys.argv[1], store.DIRECTORY_FLAGS)
    wakeup_reader, wakeup_writer = os.pipe()
    os.set_blocking(wakeup_writer, False)
    signal.set_wakeup_fd(wakeup_writer)
    signal.signal(signal.SIGTERM, store.terminate)
    try:
        scratch = store.scratch_file(directory)
        os.write(scratch.descriptor, b"incomplete")
        os.write(sys.stdout.fileno(), b"ready\n")
        os.read(wakeup_reader, 1)
    finally:
        store.discard_all()
        signal.set_wakeup_fd(-1)
        os.close(wakeup_reader)
        os.close(wakeup_writer)
        os.close(directory)


if __name__ == "__main__":
    main()
