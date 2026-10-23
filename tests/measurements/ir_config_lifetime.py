import gc
import json
import os
import resource
import subprocess
import sys
import threading
import time
import tracemalloc
import weakref
from typing import cast

from pydantic import BaseModel

from kora.ir import (
    BranchCase,
    BranchConfig,
    CommandConfig,
    CommandResource,
    FanOutConfig,
    McpServer,
    NetworkPolicy,
    NodeLimits,
    Retry,
    SubgraphNodeConfig,
    Workspace,
)
from tests.ir.config_cases import MINIMAL, POPULATED, RESOURCE
from tests.ir.control_cases import POPULATED as CONTROL_POPULATED
from tests.measurements.harness import current_rss_bytes, open_descriptors
from tests.measurements.ir_config_workload import validate_config

INVALID: dict[type[BaseModel], dict[str, object]] = {
    BranchCase: {"when": "true", "to": [False]},
    BranchConfig: {"cases": [{"when": "true", "to": ["a"]}], "else": [False]},
    FanOutConfig: {"body": "body", "over": "items", "max_parallel": 0},
    SubgraphNodeConfig: {"body": "body\n"},
    Retry: {"retry_on": ["timeout", "timeout"]},
    NodeLimits: {"max_events": 0},
    Workspace: {"repo": False},
    NetworkPolicy: {"egress": [False]},
    McpServer: {"type": "invalid"},
    CommandResource: {**RESOURCE, "size": -1},
    CommandConfig: {**MINIMAL[CommandConfig], "cmd": [False]},
}
CONFIGURATIONS: dict[type[BaseModel], dict[str, object]] = {
    **POPULATED,
    **CONTROL_POPULATED,
}


def model_references(value: object) -> list[weakref.ReferenceType[BaseModel]]:
    if isinstance(value, BaseModel):
        return [weakref.ref(value)] + [
            reference
            for name in type(value).model_fields
            for reference in model_references(getattr(value, name))
        ]
    if isinstance(value, tuple):
        return [
            reference
            for item in cast("tuple[object, ...]", value)
            for reference in model_references(item)
        ]
    return []


def cycle() -> int:
    references: list[weakref.ReferenceType[BaseModel]] = []
    for model, document in CONFIGURATIONS.items():
        parsed = model.model_validate(document)
        references.extend(model_references(parsed))
        parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
        validate_config(model, INVALID[model], False)
        del parsed
    return sum(reference() is not None for reference in references)


def rss_bytes() -> int:
    current = current_rss_bytes()
    if current is not None:
        return current
    completed = subprocess.run(
        ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
        check=True,
        capture_output=True,
    )
    return int(completed.stdout.strip()) * 1024


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def main() -> None:
    for _ in range(3):
        assert cycle() == 0
    checkpoints: list[dict[str, object]] = []
    baseline_rss = rss_bytes()
    baseline_peak_rss = peak_rss_bytes()
    baseline_threads = threading.active_count()
    baseline_descriptors = open_descriptors()
    gc.collect()
    tracemalloc.start()
    baseline_snapshot = tracemalloc.take_snapshot()
    baseline = tracemalloc.get_traced_memory()[0]
    started = time.perf_counter()
    live_models = 0

    for cycles in range(1, 1001):
        live_models += cycle()
        if cycles % 250 == 0:
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            snapshot = tracemalloc.take_snapshot()
            differences = snapshot.compare_to(baseline_snapshot, "lineno")
            checkpoints.append(
                {
                    "cycles": cycles,
                    "seconds": time.perf_counter() - started,
                    "retained_bytes": current - baseline,
                    "peak_traced_bytes": peak,
                    "current_rss_bytes": rss_bytes(),
                    "peak_rss_bytes": peak_rss_bytes(),
                    "live_models": live_models,
                    "threads": threading.active_count(),
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
            )
            del snapshot, differences
    tracemalloc.stop()
    result: dict[str, object] = {
        "baseline_rss_bytes": baseline_rss,
        "baseline_peak_rss_bytes": baseline_peak_rss,
        "baseline_threads": baseline_threads,
        "baseline_descriptors": baseline_descriptors,
        "checkpoints": checkpoints,
        "cycles": 1000,
        "models_per_cycle": len(CONFIGURATIONS),
        "warm_up": 3,
        "rss_source": "/bin/ps rss KiB converted to bytes"
        if sys.platform == "darwin"
        else "/proc/self/statm resident pages converted to bytes",
    }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
