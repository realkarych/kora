import gc
import importlib.metadata
import json
import os
import sys
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime

from kora.observability import LoggingSettings, configure_process
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import LOADING_COMPONENT, discover_providers, load_provider
from kora.protocols.metadata import ProtocolName

WARMUP: int = 5
MEASURED: int = 50
RUNTIME: ProtocolName = ProtocolName.RUNTIME


def seconds_per_call(action: Callable[[], object]) -> list[float]:
    for _ in range(WARMUP):
        action()
    timings: list[float] = []
    for _ in range(MEASURED):
        started = time.perf_counter()
        action()
        timings.append(time.perf_counter() - started)
    return timings


def refused(action: Callable[[], object]) -> Callable[[], object]:
    def attempt() -> object:
        try:
            action()
        except KoraProtocolError as error:
            return error.code
        raise AssertionError("the provider loaded")

    return attempt


def resources() -> dict[str, int]:
    gc.collect()
    heap, _ = tracemalloc.get_traced_memory()
    return {
        "modules": len(sys.modules),
        "descriptors": len(os.listdir("/dev/fd")),
        "heap_bytes": heap,
    }


def main(arguments: list[str]) -> None:
    site, log_path = arguments
    installed = sum(1 for _ in importlib.metadata.distributions())
    sys.path.insert(0, site)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    diagnostics = configure_process(
        LOADING_COMPONENT,
        LoggingSettings(),
        "loading-cost",
        lambda: datetime.now(UTC),
        descriptor,
    )
    started = time.perf_counter()
    load_provider(RUNTIME, "cost_00", diagnostics)
    cold = time.perf_counter() - started
    discover_providers(diagnostics)
    workloads: dict[str, Callable[[], object]] = {
        "load_provider": lambda: load_provider(RUNTIME, "cost_01", diagnostics),
        "not_installed": refused(lambda: load_provider(RUNTIME, "absent", diagnostics)),
        "raising_factory": refused(
            lambda: load_provider(RUNTIME, "raising", diagnostics)
        ),
        "discover_providers": lambda: discover_providers(diagnostics),
    }
    timings = {name: seconds_per_call(action) for name, action in workloads.items()}
    tracemalloc.start()
    before = resources()
    for action in workloads.values():
        seconds_per_call(action)
    after = resources()
    tracemalloc.stop()
    providers = len(discover_providers(diagnostics).providers)
    drained = diagnostics.shutdown(5.0)
    os.close(descriptor)
    sys.stdout.write(
        json.dumps(
            {
                "cold_load_provider": cold,
                **timings,
                "providers": providers,
                "installed_distributions": installed,
                "before": before,
                "after": after,
                "drained": drained,
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
