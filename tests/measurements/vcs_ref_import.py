import gc
import json
import resource
import sys
import threading
import time
import tracemalloc
from pathlib import Path

from tests.measurements.harness import current_rss_bytes, open_descriptors


def run(source: str) -> dict[str, str | float | int | None]:
    sys.path.insert(0, source)
    gc.collect()
    before_descriptors = open_descriptors()
    before_threads = threading.active_count()
    before_rss = current_rss_bytes()
    tracemalloc.start()
    before_heap = tracemalloc.get_traced_memory()[0]

    started = time.perf_counter()
    import kora.ir

    seconds = time.perf_counter() - started
    gc.collect()
    heap, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    imported = Path(kora.ir.__file__).resolve()
    if not imported.is_relative_to(Path(source).resolve()):
        raise ValueError("cold import did not use the selected source tree")
    return {
        "seconds": seconds,
        "heap_delta": heap - before_heap,
        "heap_peak": peak,
        "rss_before": before_rss,
        "rss_after": current_rss_bytes(),
        "high_water": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1024 if sys.platform == "linux" else 1),
        "descriptors_before": before_descriptors,
        "descriptors_after": open_descriptors(),
        "threads_before": before_threads,
        "threads_after": threading.active_count(),
        "source": str(imported),
        "python": sys.version,
        "platform": sys.platform,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise ValueError("expected the selected source directory")
    sys.stdout.write(json.dumps(run(sys.argv[1])) + "\n")
