import gc
import importlib.metadata
import json
import os
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime

from kora.observability import (
    CATALOG,
    LoggingConfigurationError,
    configure_process,
    read_logging_settings,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProtocolName, ProviderMetadata

FIXTURE_PREFIX: str = "kora_loading_fixture_"
CYCLES: int = 100
DISCOVERY_CYCLES: int = 20


def fixture_modules() -> list[str]:
    return sorted(name for name in sys.modules if name.startswith(FIXTURE_PREFIX))


def live_count(kinds: tuple[type, ...]) -> int:
    gc.collect()
    return sum(1 for item in gc.get_objects() if isinstance(item, kinds))


def code_of(action: Callable[[], object]) -> str:
    try:
        action()
    except KoraProtocolError as error:
        return error.code
    return "loaded"


def run_legs(log_path: str) -> dict[str, object]:
    from kora.protocols.loading import (
        LOADING_COMPONENT,
        ProviderEntry,
        discover_providers,
        load_provider,
    )

    settings = read_logging_settings(os.environ, CATALOG.components())
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    diagnostics = configure_process(
        LOADING_COMPONENT,
        settings,
        "loading-probe",
        lambda: datetime.now(UTC),
        descriptor,
    )
    runtime = ProtocolName.RUNTIME
    harness = ProtocolName.HARNESS
    result: dict[str, object] = {}

    alpha = load_provider(runtime, "alpha", diagnostics)
    result["after_alpha"] = fixture_modules()
    storage = load_provider(ProtocolName.ARTIFACT_STORAGE, "alpha", diagnostics)
    result["protocols"] = [alpha.protocol.value, storage.protocol.value]
    result["after_storage"] = fixture_modules()
    result["codes"] = {
        "runtime.gamma": code_of(lambda: load_provider(runtime, "gamma", diagnostics)),
        "runtime.delta": code_of(lambda: load_provider(runtime, "delta", diagnostics)),
        "harness.fake": code_of(lambda: load_provider(harness, "fake", diagnostics)),
        "runtime.fake": code_of(lambda: load_provider(runtime, "fake", diagnostics)),
        "runtime.probe": code_of(lambda: load_provider(runtime, "probe", diagnostics)),
        "harness.probe": code_of(lambda: load_provider(harness, "probe", diagnostics)),
        "runtime.malformed": code_of(
            lambda: load_provider(runtime, "malformed", diagnostics)
        ),
        "runtime.epsilon": code_of(
            lambda: load_provider(runtime, "epsilon", diagnostics)
        ),
    }
    result["after_refusals"] = fixture_modules()

    tracked = (ProviderMetadata, ProviderEntry, importlib.metadata.EntryPoint)
    counts: list[tuple[int, int]] = []
    for cycle in range(1, CYCLES + 1):
        load_provider(runtime, "alpha", diagnostics)
        if cycle in (1, CYCLES):
            counts.append((len(sys.modules), live_count(tracked)))
    result["load_cycles"] = counts

    failure_tracked = (*tracked, KoraProtocolError, types.TracebackType)
    failure_counts: list[tuple[int, int]] = []
    failure_codes: set[str] = set()
    for cycle in range(1, CYCLES + 1):
        failure_codes.add(
            code_of(lambda: load_provider(runtime, "broken", diagnostics))
        )
        failure_codes.add(
            code_of(lambda: load_provider(runtime, "raising", diagnostics))
        )
        if cycle in (1, CYCLES):
            failure_counts.append((len(sys.modules), live_count(failure_tracked)))
    result["failure_cycles"] = failure_counts
    result["failure_codes"] = sorted(failure_codes)

    discovery_counts: list[tuple[int, int]] = []
    for cycle in range(1, DISCOVERY_CYCLES + 1):
        discover_providers(diagnostics)
        if cycle in (1, DISCOVERY_CYCLES):
            discovery_counts.append((len(sys.modules), live_count(failure_tracked)))
    result["discovery_cycles"] = discovery_counts

    discovery = discover_providers(diagnostics)
    result["discovered"] = sorted(
        f"{provider.protocol.value}.{provider.name}"
        for provider in discovery.providers
        if provider.version.startswith("fixture")
    )
    result["discovery_failures"] = sorted(
        failure.error.code
        for failure in discovery.failures
        if failure.distribution is not None
        and failure.distribution.startswith("kora-loading-fixture")
    )
    result["drained"] = diagnostics.shutdown(5.0)
    os.close(descriptor)
    return result


def run_settings_first() -> dict[str, object]:
    try:
        read_logging_settings(os.environ, CATALOG.components())
    except LoggingConfigurationError:
        refused_before_import = True
    else:
        refused_before_import = False
    import kora.protocols.loading

    accepted = read_logging_settings(os.environ, CATALOG.components())
    return {
        "refused_before_import": refused_before_import,
        "accepted_after_import": kora.protocols.loading.LOADING_COMPONENT
        in accepted.component_levels,
    }


def main(arguments: list[str]) -> None:
    mode, first, second, log_path = arguments
    sys.path[:0] = [first, second]
    result = run_legs(log_path) if mode == "legs" else run_settings_first()
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
