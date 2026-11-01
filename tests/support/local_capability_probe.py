import asyncio
import sys
import threading
from pathlib import Path

from kora.plugins.runtime.local.configuration import LocalRuntimeConfiguration


class ResourceAudit:
    def __init__(self) -> None:
        self.count: int = 0

    def observe(self, event: str, _arguments: tuple[object, ...]) -> None:
        if event == "open" or event.startswith(
            ("os.", "pathlib.", "socket.", "subprocess.", "ctypes.")
        ):
            self.count += 1


def has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def main() -> None:
    control = Path(sys.argv[1])
    missing = sys.argv[2]
    threads = threading.enumerate()
    audit = ResourceAudit()
    sys.addaudithook(audit.observe)

    default = LocalRuntimeConfiguration(state_root=None).capabilities()
    configured = LocalRuntimeConfiguration(state_root=missing).capabilities()
    resource_events = audit.count
    unchanged_threads = threading.enumerate() == threads
    operational_imports = any(
        name == "kora.plugins.runtime.local.runtime"
        or name.startswith(("kora.runner", "kora.executor"))
        for name in sys.modules
    )
    running_loop = has_running_loop()
    control.read_bytes()
    observed_control = audit.count > resource_events

    if (
        resource_events != 0
        or not unchanged_threads
        or operational_imports
        or running_loop
        or not observed_control
        or default != configured
    ):
        raise RuntimeError("local capability inspection boundary failed")
    sys.stdout.write("pure local capability source verified\n")


if __name__ == "__main__":
    main()
