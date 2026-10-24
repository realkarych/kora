import json
import os
import resource
import sys
import time
import tracemalloc


def run() -> int:
    scope = os.environ["KORA_MEASUREMENT_TRACE_SCOPE"]
    if scope not in ("cold_import", "mutation"):
        raise ValueError("unknown helper trace scope")
    if tracemalloc.is_tracing() != (scope == "cold_import"):
        raise RuntimeError("helper trace scope does not match interpreter tracing")
    started = time.perf_counter_ns()
    from kora.conformance.io_helper import main

    imported = time.perf_counter_ns()
    if scope == "mutation":
        tracemalloc.start(1)
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
                "trace_scope": scope,
                **(
                    {"cold_current_bytes": current, "cold_peak_bytes": peak}
                    if scope == "cold_import"
                    else {
                        "mutation_current_bytes": current,
                        "mutation_peak_bytes": peak,
                    }
                ),
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
