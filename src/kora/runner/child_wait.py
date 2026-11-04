import asyncio
import ctypes
import os
import select
import signal
import sys
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol, cast

from kora.protocols.lifetime_records import ProcessRegistration
from kora.runner.identity import (
    DARWIN_ZOMBIE,
    PROC_PIDTBSDINFO,
    BsdInfo,
    ProcPidInfo,
    complete_identity,
)


class ChildWatch(Protocol):
    @property
    def changed(self) -> asyncio.Future[None]: ...

    def close(self) -> None: ...


class KernelChildWatch:
    def __init__(self, pid: int) -> None:
        self._loop = asyncio.get_running_loop()
        self._descriptor, self._release = child_change_descriptor(pid)
        self._changed: asyncio.Future[None] = self._loop.create_future()
        try:
            self._loop.add_reader(self._descriptor, self._notify)
        except OSError:
            self._release()
            raise
        self._reading = True
        self._closed = False

    @property
    def changed(self) -> asyncio.Future[None]:
        return self._changed

    def _notify(self) -> None:
        self._stop_reading()
        if not self._changed.done():
            self._changed.set_result(None)

    def _stop_reading(self) -> None:
        if self._reading:
            self._loop.remove_reader(self._descriptor)
            self._reading = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_reading()
        self._release()
        self._changed.cancel()


def child_change_descriptor(pid: int) -> tuple[int, Callable[[], None]]:
    if sys.platform != "darwin":
        descriptor = os.pidfd_open(pid)
        return descriptor, lambda: os.close(descriptor)
    queue = select.kqueue()
    try:
        queue.control(
            [
                select.kevent(
                    signal.SIGCHLD,
                    filter=select.KQ_FILTER_SIGNAL,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                )
            ],
            0,
            0,
        )
    except OSError:
        queue.close()
        raise
    return queue.fileno(), queue.close


class Readiness(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


class DirectChildProbe(Protocol):
    def read(self, registration: ProcessRegistration) -> Readiness: ...


class DarwinDirectChildProbe:
    def __init__(
        self,
        *,
        parent_pid: int | None = None,
        read_info: ProcPidInfo | None = None,
    ) -> None:
        self._parent = os.getpid() if parent_pid is None else parent_pid
        self._read_info = read_info
        if read_info is None and sys.platform == "darwin":
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            library.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            library.proc_pidinfo.restype = ctypes.c_int
            self._read_info = cast(ProcPidInfo, library.proc_pidinfo)

    def read(self, registration: ProcessRegistration) -> Readiness:
        reader = self._read_info
        process = registration.process
        if reader is None or not complete_identity(process):
            return Readiness.UNKNOWN
        info = BsdInfo()
        ctypes.set_errno(0)
        try:
            result = reader(
                process.pid,
                PROC_PIDTBSDINFO,
                1,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        except OSError:
            return Readiness.UNKNOWN
        if result != ctypes.sizeof(info):
            return Readiness.UNKNOWN
        return classify_direct_child(info, registration, self._parent)


def classify_direct_child(
    info: BsdInfo, registration: ProcessRegistration, parent_pid: int
) -> Readiness:
    process = registration.process
    start = info.start_seconds * 1_000_000 + info.start_microseconds
    if (
        info.pid != process.pid
        or start != process.start_time
        or info.process_group_id != registration.process_group_id
        or info.parent_pid != parent_pid
    ):
        return Readiness.MISMATCH
    if info.status == DARWIN_ZOMBIE:
        return Readiness.READY
    return Readiness.NOT_READY


def native_direct_child_wait() -> tuple[
    DirectChildProbe | None, Callable[[int], ChildWatch] | None
]:
    if sys.platform != "darwin":
        return None, None
    return DarwinDirectChildProbe(), KernelChildWatch
