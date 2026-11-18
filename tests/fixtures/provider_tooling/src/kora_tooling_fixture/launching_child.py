import json
import os
import signal
import sys
from pathlib import Path


def main() -> int:
    placements = [Path(value) for value in sys.argv[1:]]
    for directory in placements:
        (directory / "accessed").write_text("child")
    sys.stdout.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "placements": [str(path) for path in placements],
            }
        )
        + "\n"
    )
    sys.stdout.flush()
    for command in sys.stdin:
        action = command.strip()
        if action in {"allowed", "denied"}:
            Path(action).write_text("executed")
            sys.stdout.write(action + "\n")
            sys.stdout.flush()
        elif action == "hold":
            sys.stdout.write("hold\n")
            sys.stdout.flush()
            signal.pause()
            return 0
        elif action == "exit":
            return 0
        elif action == "nonzero":
            return 7
        else:
            return 9
    return 0


if __name__ == "__main__":
    sys.exit(main())
