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
    protocol: ProtocolName, name: str
) -> Callable[[], ProviderMetadata]:
    def factory() -> ProviderMetadata:
        return ProviderMetadata(
            protocol, name, implementation, {"type": "object"}, "1.0"
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
) -> ProviderEntry:
    return ProviderEntry(
        name=name,
        value=f"{module}:provider",
        module=module,
        distribution=DISTRIBUTION,
        distribution_version=DISTRIBUTION_VERSION,
        load=load,
    )


def source(*entries: ProviderEntry) -> FakeEntryPoints:
    return FakeEntryPoints(entries)
