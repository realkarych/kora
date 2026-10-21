from collections.abc import Callable, Iterator
from dataclasses import dataclass

from kora.protocols.loading import EntryPointSource, ProviderEntry
from kora.protocols.metadata import ProtocolName, ProviderMetadata

DISTRIBUTION: str = "kora-loading-fixture"
DISTRIBUTION_VERSION: str = "1.0"


@dataclass(frozen=True)
class FakeEntryPoints(EntryPointSource):
    provided: tuple[ProviderEntry, ...]

    def entries(self) -> Iterator[ProviderEntry]:
        yield from self.provided


def implementation(configuration: object) -> object:
    return configuration


def metadata_factory(
    protocol: ProtocolName, name: str, version: str = "1.0"
) -> Callable[[], ProviderMetadata]:
    def factory() -> ProviderMetadata:
        return ProviderMetadata(
            protocol, name, implementation, {"type": "object"}, version
        )

    return factory


def returning(value: object) -> Callable[[], object]:
    def load() -> object:
        return value

    return load


def entry(
    name: str,
    load: Callable[[], object],
    module: str | None = "kora_loading_fixture.providers",
    value: str | None = None,
    distribution: str | None = DISTRIBUTION,
    distribution_version: str | None = DISTRIBUTION_VERSION,
) -> ProviderEntry:
    return ProviderEntry(
        name=name,
        value=value if value is not None else f"{module}:provider",
        module=module,
        distribution=distribution,
        distribution_version=distribution_version,
        load=load,
    )


def source(*entries: ProviderEntry) -> FakeEntryPoints:
    return FakeEntryPoints(entries)
