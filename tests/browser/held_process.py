import ctypes
import os
import sys


def hold_process() -> None:
    if sys.argv[1] == "denied":
        control = ctypes.CDLL(None, use_errno=True).prctl
        control.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
        control.restype = ctypes.c_int
        if control(4, 0, 0, 0, 0) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    sys.stdin.buffer.read(1)


if __name__ == "__main__":
    hold_process()
