import gc
import json
import os
import sys
import threading
import time
import tracemalloc
from typing import cast

from tests.measurements.harness import (
    open_descriptors,
    os_threads,
    peak_rss_bytes,
    rss_bytes,
)
from tests.measurements.ir_schema_workload import (
    schema_cycle,
    schema_fixture_manifest,
    schema_models,
)


def main() -> None:
    for action in range(3):
        for _ in range(3):
            schema_cycle(action)
    manifest = cast("dict[str, object]", json.loads(schema_fixture_manifest()))
    models = schema_models()
    baseline_schemas = {
        name: id(model.__pydantic_core_schema__) for name, model in models.items()
    }
    checkpoints: list[dict[str, object]] = [{} for _ in range(4)]
    counts = [0, 0, 0]
    baseline_rss = rss_bytes()
    baseline_peak_rss = peak_rss_bytes()
    baseline_threads = threading.active_count()
    baseline_os_threads = os_threads()
    baseline_descriptors = open_descriptors()
    gc.collect()
    tracemalloc.start()
    baseline_snapshot = tracemalloc.take_snapshot()
    baseline = tracemalloc.get_traced_memory()[0]
    started = time.perf_counter()

    for cycles in range(1, 1001):
        schema_cycle(cycles - 1)
        counts[(cycles - 1) % 3] += 1
        if cycles % 250 == 0:
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            snapshot = tracemalloc.take_snapshot()
            differences = snapshot.compare_to(baseline_snapshot, "lineno")
            checkpoints[cycles // 250 - 1] = {
                "cycles": cycles,
                "seconds": time.perf_counter() - started,
                "retained_bytes": current - baseline,
                "peak_traced_bytes": peak,
                "current_rss_bytes": rss_bytes(),
                "peak_rss_bytes": peak_rss_bytes(),
                "threads": threading.active_count(),
                "os_threads": os_threads(),
                "tracer_bytes": tracemalloc.get_tracemalloc_memory(),
                "descriptors": open_descriptors(),
                "allocation_differences": [
                    {
                        "location": str(item.traceback),
                        "bytes": item.size_diff,
                        "count": item.count_diff,
                    }
                    for item in differences[:10]
                ],
            }
            del snapshot, differences
    tracemalloc.stop()
    assert {
        name: id(model.__pydantic_core_schema__) for name, model in models.items()
    } == baseline_schemas
    result: dict[str, object] = {
        "revision": sys.argv[1],
        "pid": os.getpid(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "baseline_rss_bytes": baseline_rss,
        "baseline_peak_rss_bytes": baseline_peak_rss,
        "baseline_threads": baseline_threads,
        "baseline_os_threads": baseline_os_threads,
        "baseline_descriptors": baseline_descriptors,
        "checkpoints": checkpoints,
        "cycles": 1000,
        "action_counts": dict(
            zip(("validation", "serialization", "render_utf8"), counts, strict=True)
        ),
        "class_schema_inventory": sorted(baseline_schemas),
        "class_schemas_unchanged": True,
        "schema_manifest": manifest,
        "warm_up": 3,
        "rss_source": "/bin/ps rss KiB converted to bytes"
        if sys.platform == "darwin"
        else "/proc/self/statm resident pages converted to bytes",
    }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise ValueError("expected exact measurement revision")
    main()
