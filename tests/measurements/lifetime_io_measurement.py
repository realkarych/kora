import json
import os
import resource
import sys
import time
import tracemalloc


def run() -> int:
    if not tracemalloc.is_tracing():
        raise RuntimeError("interpreter allocation tracing required")
    started = time.perf_counter_ns()
    from kora.conformance.io_helper import main

    imported = time.perf_counter_ns()
    code = main()
    current, peak = tracemalloc.get_traced_memory()
    native_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    encoded = (
        json.dumps(
            {
                "pid": os.getpid(),
                "code": code,
                "import_ns": imported - started,
                "total_ns": time.perf_counter_ns() - started,
                "heap_bytes": current,
                "heap_peak_bytes": peak,
                "native_peak_bytes": native_peak
                * (1024 if sys.platform == "linux" else 1),
                "imports": len(sys.modules),
                "descriptors_before_kernel_exit": len(os.listdir("/dev/fd")),
            },
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    if len(encoded) > 4096:
        raise ValueError("helper measurement record exceeds bound")
    descriptor = int(os.environ["KORA_MEASUREMENT_FD"])
    if os.write(descriptor, encoded) != len(encoded):
        raise OSError("incomplete helper measurement record")
    os.close(descriptor)
    return code


if __name__ == "__main__":
    raise SystemExit(run())
