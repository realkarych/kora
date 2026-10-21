import json
from collections.abc import Callable

import pytest

from kora.observability import Level, LoggingSettings, ProcessDiagnostics
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import (
    LOADING_COMPONENT,
    Discovery,
    discover_providers,
    load_provider,
)
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from tests.protocols.conftest import DRAIN_SECONDS, Configure
from tests.protocols.entry_points import (
    DISTRIBUTION,
    FakeEntryPoints,
    entry,
    metadata_factory,
    returning,
    source,
)
from tests.support.observability import Pipe

SENTINEL: str = "sentinel-7f3a91c2"
SENTINEL_MODULE: str = "kora_loading_fixture.sentinel_7f3a91c2"
RUNTIME: ProtocolName = ProtocolName.RUNTIME
DEBUG_LOADING: LoggingSettings = LoggingSettings(
    component_levels={LOADING_COMPONENT: Level.DEBUG}
)


def raising_import() -> object:
    raise ImportError(SENTINEL)


def raising_factory() -> object:
    raise RuntimeError(SENTINEL)


def failing_entries() -> FakeEntryPoints:
    return source(
        entry(
            "runtime.alpha",
            returning(metadata_factory(RUNTIME, "alpha")),
            module=SENTINEL_MODULE,
        ),
        entry("runtime.broken", raising_import, module=SENTINEL_MODULE),
        entry("runtime.raising", returning(raising_factory), module=SENTINEL_MODULE),
        entry(f"runtime.Bad-{SENTINEL}", raising_import, module=SENTINEL_MODULE),
    )


def finish(diagnostics: ProcessDiagnostics, pipe: Pipe) -> list[dict[str, object]]:
    assert diagnostics.shutdown(DRAIN_SECONDS)
    pipe.close_write_end()
    pipe.reader.wait_closed()
    return pipe.reader.records()


def refusal(action: Callable[[], ProviderMetadata]) -> KoraProtocolError:
    with pytest.raises(KoraProtocolError) as raised:
        action()
    return raised.value


def assert_content_free(error: KoraProtocolError) -> None:
    assert error.retryable is False
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = f"{error} {error!r} {json.dumps(error.details)}"
    assert SENTINEL not in rendered
    assert SENTINEL_MODULE not in rendered
    assert DISTRIBUTION not in rendered


def test_provider_loading_failure_is_structured_and_logged(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(DEBUG_LOADING, pipe.write_end)
    pipe.reader.start()
    entries = failing_entries()

    def load(name: str) -> Callable[[], ProviderMetadata]:
        return lambda: load_provider(RUNTIME, name, diagnostics, entries)

    missing = refusal(load("missing"))
    invalid = refusal(load(f"Bad-{SENTINEL}"))
    broken = refusal(load("broken"))
    raising = refusal(load("raising"))
    loaded = load("alpha")()
    records = finish(diagnostics, pipe)

    assert missing.code == "provider_not_installed"
    assert missing.details == {"protocol": "runtime", "name": "missing"}
    assert invalid.code == "provider_name_invalid"
    assert invalid.details == {
        "protocol": "runtime",
        "name_length": len(f"Bad-{SENTINEL}"),
    }
    assert broken.code == "provider_load_failed"
    assert broken.details["exception_type"] == "builtins.ImportError"
    assert raising.code == "provider_load_failed"
    assert raising.details["exception_type"] == "builtins.RuntimeError"
    for error in (missing, invalid, broken, raising):
        assert_content_free(error)
    assert loaded.name == "alpha"
    assert [record["event"] for record in records] == [
        "protocols.loading.provider_loaded"
    ]
    assert records[0]["component"] == LOADING_COMPONENT
    assert records[0]["distribution"] == DISTRIBUTION
    assert SENTINEL not in json.dumps(records)
    assert SENTINEL_MODULE not in json.dumps(records)


def test_provider_loading_failure_is_structured_and_logged_in_discovery(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(DEBUG_LOADING, pipe.write_end)
    pipe.reader.start()

    discovery: Discovery = discover_providers(diagnostics, failing_entries())
    records = finish(diagnostics, pipe)

    assert [provider.name for provider in discovery.providers] == ["alpha"]
    assert [failure.error.code for failure in discovery.failures] == [
        "provider_load_failed",
        "provider_load_failed",
        "provider_name_invalid",
    ]
    for failure in discovery.failures:
        assert_content_free(failure.error)
    unavailable = [
        record
        for record in records
        if record["event"] == "protocols.loading.provider_unavailable"
    ]
    assert len(unavailable) == 3
    assert {record["component"] for record in unavailable} == {LOADING_COMPONENT}
    assert {record["level"] for record in unavailable} == {"WARNING"}
    assert unavailable[0]["exception_type"] == "builtins.ImportError"
    assert unavailable[2]["entry_name_length"] == len(f"runtime.Bad-{SENTINEL}")
    assert "provider" not in unavailable[2]
    assert SENTINEL not in json.dumps(records)
    assert SENTINEL_MODULE not in json.dumps(records)
