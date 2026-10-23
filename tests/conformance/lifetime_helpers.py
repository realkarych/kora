import socket
import subprocess
import sys
from pathlib import Path

from kora.conformance.io_helper import InstalledIOHelperLauncher, IOHelper
from kora.protocols.time import Clock


class ProbeIOHelperLauncher:
    def __init__(
        self,
        home: Path,
        observation_descriptor: int,
        fifo_directory: int | None = None,
        phase: str = "before",
        pause_at: int | None = None,
    ) -> None:
        self.home = home
        self.observation_descriptor = observation_descriptor
        self.fifo_directory = fifo_directory
        self.phase = phase
        self.pause_at = pause_at
        self.starts = 0

    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper:
        self.starts += 1
        if self.pause_at is not None and self.starts != self.pause_at:
            return InstalledIOHelperLauncher(
                home=self.home, state_home=self.home / "state"
            ).start(descriptors, clock)
        parent, child = socket.socketpair()
        inherited = (child.fileno(), self.observation_descriptor, *descriptors)
        environment = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / "state"),
            "KORA_IO_CONTROL_FD": str(child.fileno()),
            "KORA_IO_DESCRIPTORS": ",".join(str(value) for value in descriptors),
            "KORA_TEST_OBSERVATION_FD": str(self.observation_descriptor),
            "KORA_TEST_PAUSE_PHASE": self.phase,
        }
        if self.fifo_directory is not None:
            inherited = (*inherited, self.fifo_directory)
            environment["KORA_TEST_FIFO_DIRECTORY_FD"] = str(self.fifo_directory)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("lifetime_io_probe.py")),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
                pass_fds=inherited,
                process_group=0,
            )
        except OSError:
            parent.close()
            raise
        finally:
            child.close()
        return IOHelper(process, parent, clock)
