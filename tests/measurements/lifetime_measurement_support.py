import ctypes
import errno
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from kora.conformance.io_helper import IOHelper
from kora.protocols.time import Clock
from kora.runner.identity import ProcPidInfo


class HelperMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int
    code: int
    import_ns: int
    total_ns: int
    heap_bytes: int
    heap_peak_bytes: int
    native_peak_bytes: int
    imports: int
    descriptors_before_kernel_exit: int


class MeasuredIOHelperLauncher:
    def __init__(self, home: Path, measurement_descriptor: int) -> None:
        self.home = home
        self.measurement_descriptor = measurement_descriptor
        self.starts = 0

    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper:
        parent, child = socket.socketpair()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "tracemalloc=1",
                    "-I",
                    str(Path(__file__).with_name("lifetime_io_measurement.py")),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    "HOME": str(self.home),
                    "XDG_STATE_HOME": str(self.home / "state"),
                    "KORA_IO_CONTROL_FD": str(child.fileno()),
                    "KORA_IO_DESCRIPTORS": ",".join(str(item) for item in descriptors),
                    "KORA_MEASUREMENT_FD": str(self.measurement_descriptor),
                },
                close_fds=True,
                pass_fds=(child.fileno(), self.measurement_descriptor, *descriptors),
                process_group=0,
            )
        except OSError:
            parent.close()
            raise
        finally:
            child.close()
        self.starts += 1
        return IOHelper(process, parent, clock)


class TaskInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("context_switches", ctypes.c_int32),
        ("thread_count", ctypes.c_int32),
        ("running_threads", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]
    resident_size: int
    thread_count: int


class NativeCounters:
    def __init__(self) -> None:
        self.library: ctypes.CDLL | None = None
        if sys.platform == "darwin":
            self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.library.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.library.proc_pidinfo.restype = ctypes.c_int
        elif sys.platform != "linux":
            raise RuntimeError("unsupported measurement platform")

    def read(self, pid: int) -> tuple[int, int]:
        if self.library is not None:
            info = TaskInfo()
            query = cast(ProcPidInfo, self.library.proc_pidinfo)
            returned = query(pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info))
            if returned != ctypes.sizeof(info):
                raise OSError(ctypes.get_errno(), "native task counter unavailable")
            return info.resident_size, info.thread_count
        root = Path("/proc") / str(pid)
        resident = int((root / "statm").read_text().split()[1])
        return resident * os.sysconf("SC_PAGE_SIZE"), len(
            tuple((root / "task").iterdir())
        )


def descriptor_kinds() -> dict[str, int]:
    counts = {"file": 0, "directory": 0, "pipe": 0, "socket": 0, "other": 0}
    for name in os.listdir("/dev/fd"):
        try:
            mode = os.fstat(int(name)).st_mode
        except OSError as failure:
            if failure.errno != errno.EBADF:
                raise
            continue
        kind = (
            "file"
            if stat.S_ISREG(mode)
            else "directory"
            if stat.S_ISDIR(mode)
            else "pipe"
            if stat.S_ISFIFO(mode)
            else "socket"
            if stat.S_ISSOCK(mode)
            else "other"
        )
        counts[kind] += 1
    return counts
