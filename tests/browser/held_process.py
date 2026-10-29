import ctypes
import os
import sys


def set_visibility(readable: bool) -> None:
    control = ctypes.CDLL(None, use_errno=True).prctl
    control.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    control.restype = ctypes.c_int
    if control(4, int(readable), 0, 0, 0) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def hold_process() -> None:
    if sys.argv[1] == "denied":
        set_visibility(False)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while command := sys.stdin.buffer.read(1):
        if command == b"r":
            set_visibility(True)
            sys.stdout.write("readable\n")
            sys.stdout.flush()
        else:
            break


if __name__ == "__main__":
    hold_process()
