import asyncio
import os
import socket
from pathlib import Path

from kora.conformance.artifact_storage import ArtifactStorageConformanceInputs
from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.storage_support import operation, stage_spec
from kora.ir.json import JsonObject
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorage
from kora_tooling_fixture.owned_directory import dispose_owned_directory


async def failure(
    provider: ArtifactStorage, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    spec = stage_spec(provider.identity(), marker, b"")
    request = operation(provider.identity(), "wrong-binding", "begin_stage", spec)
    return await provider.begin_stage(
        request, spec.model_copy(update={"content_type": "changed"}), context
    )


async def effect(
    provider: ArtifactStorage, config: JsonObject, context: ProtocolContext
) -> None:
    root = config["root"]
    assert isinstance(root, str)
    assert Path(root).is_dir()
    spec = stage_spec(provider.identity(), "configuration", b"")
    request = operation(provider.identity(), "configuration", "begin_stage", spec)
    opened = await provider.begin_stage(request, spec, context)
    assert opened.storage_identity == provider.identity()
    assert isinstance(provider, ConfiguredStorage)
    provider.require_configuration(config)
    mode = os.environ.get("KORA_FIXTURE_MODE", "normal")
    if mode == "exercise":
        raise ValueError("private-storage-exercise-marker")
    if mode == "blocked":
        await asyncio.to_thread(block_native)


def block_native() -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.connect(os.environ["KORA_FIXTURE_FEEDBACK"])
        endpoint.sendall(b"b")
        assert endpoint.recv(1) == b"a"
        endpoint.recv(1)


class StoreReceivers:
    def __init__(self) -> None:
        self.receivers: list[ConfiguredStorage] = []
        self.identity: tuple[int, int, int] | None = None

    def retain(self, receiver: "ConfiguredStorage", descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid)
        if self.identity is None:
            self.identity = identity
        assert identity == self.identity
        self.receivers.append(receiver)

    def dispose(self, root: Path) -> None:
        assert self.receivers
        assert all(receiver.closed_confirmed for receiver in self.receivers)
        assert self.identity is not None
        dispose_owned_directory(root, self.identity)


class ConfiguredStorage(LocalArtifactStorage):
    def __init__(
        self, configuration: LocalStorageConfiguration, receivers: StoreReceivers
    ) -> None:
        super().__init__(configuration)
        self.closed_confirmed = False
        receivers.retain(self, self._root.fd)

    def require_configuration(self, config: JsonObject) -> None:
        root = LocalStorageConfiguration.model_validate(config).root
        expected = os.stat(root)
        actual = os.fstat(self._root.fd)
        assert (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)

    async def aclose(self) -> None:
        await super().aclose()
        assert not self._jobs
        assert not self._native
        assert not self._state.connections
        assert self._root.closed
        assert not self._readers.streams
        assert not self._readers.handles
        self.closed_confirmed = True


class Adapter:
    exercises: ArtifactStorageConformanceInputs = ArtifactStorageConformanceInputs(
        failure, effect
    )

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.directory: Path | None = None
        self.configuration: JsonObject = {}
        self.receivers = StoreReceivers()

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        self.loop = asyncio.get_running_loop()
        self.directory = Path.cwd()
        self.configuration = dict(config)
        self.receivers = StoreReceivers()
        parsed = LocalStorageConfiguration.model_validate(config)
        root = Path(parsed.root)
        assert not root.is_absolute()
        assert root.resolve().is_relative_to(self.directory)
        if os.environ.get("KORA_FIXTURE_MODE") == "preexisting":
            root.mkdir(mode=0o700)
            (root / "borrowed").write_bytes(b"preserve-before-parent-stop")
        assert not root.exists()
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        assert config == self.configuration
        assert asyncio.get_running_loop() is self.loop
        assert Path.cwd() == self.directory
        return ConstructorArguments(
            (LocalStorageConfiguration.model_validate(config), self.receivers)
        )

    async def cleanup(self, instance: object | None) -> None:
        assert asyncio.get_running_loop() is self.loop
        assert Path.cwd() == self.directory
        if instance is not None:
            assert isinstance(instance, ConfiguredStorage)
            await instance.aclose()
            self.receivers.dispose(
                Path(LocalStorageConfiguration.model_validate(self.configuration).root)
            )


def configured() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.ARTIFACT_STORAGE,
        "configured",
        ConfiguredStorage,
        LocalArtifactStorage.config_schema,
        "1",
    )
