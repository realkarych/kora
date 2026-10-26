import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import cast

import pytest

from kora.conformance.artifact_storage import (
    ArtifactStorageConformanceInputs,
    check_artifact_storage_staging_round_trip,
)
from kora.conformance.bridge import (
    ConformanceSelection,
    ConstructorArguments,
    PreparedConformance,
    PreparedInvocation,
)
from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.conformance.storage_cases import (
    StorageCaseError,
    StorageCaseReason,
    StorageCaseRunner,
)
from kora.conformance.transport import LifetimeClient
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorage
from kora.runner.filesystem import DirectoryAnchor, FilesystemError


async def failure(
    provider: ArtifactStorage, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    raise AssertionError("case ownership does not run the common failure driver")


async def effect(
    provider: ArtifactStorage, config: JsonObject, context: ProtocolContext
) -> None:
    raise AssertionError("case ownership does not run the configuration driver")


async def wait_forever() -> None:
    await asyncio.get_running_loop().create_future()


class ObservedStorage(LocalArtifactStorage):
    def __init__(self, configuration: LocalStorageConfiguration) -> None:
        super().__init__(configuration)
        self.loop = asyncio.get_running_loop()
        self.case_directory = Path.cwd()

    async def aclose(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        assert Path.cwd() == self.case_directory
        await super().aclose()

    def require_closed(self) -> None:
        assert not self._jobs
        assert not self._native
        assert not self._readers.streams
        assert not self._readers.handles
        assert not self._state.connections
        assert self._root.closed


class Adapter:
    def __init__(self, mode: str, root: str, feedback: socket.socket) -> None:
        self.mode = mode
        self.root = root
        self.feedback = feedback
        self.exercises = ArtifactStorageConformanceInputs(failure, effect)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.directories: list[Path] = []
        self.receivers: list[ObservedStorage] = []
        self.cleaned: list[object | None] = []
        self.configurations: list[JsonObject] = []
        self.native_results: list[bool] = []
        self.started: asyncio.Future[None] | None = None

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        loop = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = loop
        assert loop is self.loop
        self.receivers = []
        self.directories.append(Path.cwd())
        self.configurations.append(config)
        self.feedback.send(b"case:" + Path.cwd().name.encode())
        assert config["root"] == self.root
        assert not Path(self.root).exists()
        assert await asyncio.to_thread(Path.cwd) == Path.cwd()
        if self.mode == "prepare":
            raise ValueError("prepare")
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        assert asyncio.get_running_loop() is self.loop
        assert Path.cwd() == self.directories[-1]
        assert config == self.configurations[-1]
        if self.mode == "bind":
            raise ValueError("bind")
        return ConstructorArguments((LocalStorageConfiguration.model_validate(config),))

    def construct(self, configuration: LocalStorageConfiguration) -> object:
        assert asyncio.get_running_loop() is self.loop
        if self.mode == "construct":
            raise ValueError("construct")
        provider = ObservedStorage(configuration)
        self.receivers.append(provider)
        assert (Path.cwd() / self.root).is_dir()
        return provider

    async def cleanup(self, instance: object | None) -> None:
        assert asyncio.get_running_loop() is self.loop
        assert Path.cwd() == self.directories[-1]
        self.cleaned.append(instance)
        if instance is None:
            assert not self.receivers
        else:
            assert instance is self.receivers[0]
            for receiver in self.receivers[1:]:
                receiver.require_closed()
            await self.receivers[0].aclose()
            self.receivers[0].require_closed()
        if self.mode == "cleanup":
            raise ValueError("cleanup")
        if self.mode in {"native", "late", "blocked"}:
            self.feedback.send(b"cleaned")
        if self.mode == "substitute":
            current = Path.cwd()
            os.rename(current, current.with_name(current.name + "-moved"))
            current.mkdir(mode=0o700)
            (current / "foreign").write_bytes(b"preserve")

    def late_work(self) -> None:
        asyncio.create_task(wait_forever())

    def native_work(self) -> None:
        expected = self.directories[-1]
        loop = self.loop
        started = self.started
        assert loop is not None
        assert started is not None
        loop.call_soon_threadsafe(started.set_result, None)
        self.feedback.send(b"native")
        assert self.feedback.recv(16) == b"release"
        self.native_results.append(Path.cwd() == expected and expected.is_dir())
        if self.mode == "late":
            loop.call_soon_threadsafe(self.late_work)

    async def exercise(self, invocation: PreparedInvocation) -> None:
        assert invocation.instance is self.receivers[0]
        assert invocation.configuration == self.configurations[-1]
        assert asyncio.get_running_loop() is self.loop
        if self.mode == "exercise":
            raise ValueError("exercise")
        if self.mode == "cancel":
            raise asyncio.CancelledError
        if self.mode in {"native", "late", "blocked"}:
            self.started = asyncio.get_running_loop().create_future()
            waiting = asyncio.create_task(asyncio.to_thread(self.native_work))
            await self.started
            waiting.cancel()
            try:
                await waiting
            except asyncio.CancelledError:
                assert waiting.cancelled()
        elif self.mode == "task":
            asyncio.create_task(wait_forever())
        else:
            with captured(
                CONFORMANCE_COMPONENT, LoggingSettings(), "storage-case", utc_now
            ) as capture:
                await check_artifact_storage_staging_round_trip(
                    cast("ArtifactStorage", invocation.instance),
                    context_for(capture),
                    invocation.reconstruct,
                    1,
                )


async def connected(address: str) -> LifetimeClient:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.setblocking(False)
    await asyncio.get_running_loop().sock_connect(endpoint, address)
    return LifetimeClient(endpoint)


def run(address: str, feedback_address: str, mode: str, root: str) -> None:
    feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    feedback.bind(feedback_address + "-child")
    feedback.connect(feedback_address)
    runner = asyncio.Runner()
    client = runner.run(connected(address))
    startup_path = Path.cwd()
    startup = DirectoryAnchor.open_path(startup_path)
    invocation_root = DirectoryAnchor.open_path(startup_path.parent)
    adapter = Adapter(mode, root, feedback)
    selection = ConformanceSelection(
        ProviderMetadata(
            ProtocolName.ARTIFACT_STORAGE,
            "local",
            adapter.construct,
            LocalArtifactStorage.config_schema,
            "1",
        ),
        json.dumps({"root": root, "limits": {"version": 1}}).encode(),
    )
    cases = StorageCaseRunner(
        runner, selection, adapter, client, invocation_root, startup
    )

    async def exercise(invocation: PreparedInvocation) -> None:
        await adapter.exercise(invocation)
        if mode == "restore":
            startup.close()

    if mode in {"success", "native", "blocked"}:
        for _ in range(2):
            cases.run(exercise)
            assert Path.cwd() == startup_path
            assert not adapter.directories[-1].exists()
            assert all(adapter.native_results)
            feedback.send(b"closed")
        assert len(set(adapter.directories)) == 2
        assert len(adapter.cleaned) == 2
        assert adapter.configurations[0] == adapter.configurations[1]
        client.disconnect()
        runner.close()
        startup.close()
        invocation_root.close()
        feedback.send(b"done")
        feedback.close()
    else:
        with pytest.raises(
            (ValueError, RuntimeError, FilesystemError, asyncio.CancelledError)
        ) as caught:
            cases.run(exercise)
        error = caught.value
        if mode == "substitute":
            assert isinstance(error, StorageCaseError)
            assert error.reason is StorageCaseReason.CLEANUP_UNKNOWN
            assert Path.cwd() == startup_path
        else:
            assert Path.cwd() == adapter.directories[-1]
        if mode in {"late", "task"}:
            assert isinstance(error, StorageCaseError)
            assert error.reason is StorageCaseReason.TASKS_LIVE
        if mode == "cancel":
            assert isinstance(error, asyncio.CancelledError)
        if mode in {"prepare", "bind", "construct", "exercise", "cleanup"}:
            assert str(error) == mode
        if mode == "prepare":
            assert not adapter.cleaned
        elif mode in {"bind", "construct"}:
            assert adapter.cleaned == [None]
        else:
            assert adapter.cleaned == [adapter.receivers[0]]
        with pytest.raises(StorageCaseError) as repeated:
            cases.run(exercise)
        assert repeated.value.reason is StorageCaseReason.SEQUENCE_CLOSED
        assert len(adapter.directories) == 1
        assert all(adapter.native_results)
        feedback.send(b"failed")
        runner.run(wait_forever())


if __name__ == "__main__":
    error_descriptor = os.open(
        sys.argv[2] + ".stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    os.dup2(error_descriptor, 2)
    os.close(error_descriptor)
    run(*sys.argv[1:])
