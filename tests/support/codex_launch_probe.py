import asyncio
import sys
import threading
from pathlib import Path

from kora.plugins.harness.codex.launch_configuration import (
    CodexLaunchProfile,
    NativeHomes,
    build_launch_inputs,
)


class ResourceAudit:
    def __init__(self) -> None:
        self.count: int = 0

    def observe(self, event: str, _arguments: tuple[object, ...]) -> None:
        if event == "open" or event.startswith(
            ("os.", "pathlib.", "socket.", "subprocess.", "ctypes.")
        ):
            self.count += 1


def homes(root: Path) -> NativeHomes:
    return NativeHomes(root / "home", root / "codex", root / "tmp")


def observe_construction(root: Path) -> None:
    control = root / "control"
    control.write_bytes(b"audit control")
    threads = threading.enumerate()
    audit = ResourceAudit()
    sys.addaudithook(audit.observe)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop_running = False
    else:
        loop_running = True

    profile = CodexLaunchProfile("fixture", "http://127.0.0.1:12345/v1", "read-only")
    launch = build_launch_inputs(
        profile,
        executable=Path("/nonexistent/codex"),
        bash=Path("/bin/bash"),
        workspace=root / "workspace",
        app_homes=homes(root / "app"),
        exec_homes=homes(root / "exec"),
        executor_digest="a" * 64,
    )
    events = audit.count
    control.read_bytes()

    assert events == 0
    assert audit.count > events
    assert not loop_running
    assert threading.enumerate() == threads
    assert launch.app_server.environment["HOME"] == str(root / "app" / "home")
    assert launch.exec_server.environment["HOME"] == str(root / "exec" / "home")
    assert not (root / "workspace").exists()
    sys.stdout.write("pure Codex launch construction verified\n")


if __name__ == "__main__":
    observe_construction(Path(sys.argv[1]))
