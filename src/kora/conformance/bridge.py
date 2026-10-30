import asyncio
import sys
from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypeAlias, Union, cast

from kora.conformance.inputs import ConformanceInputs
from kora.ir.json import JsonObject
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorage

if TYPE_CHECKING:
    from kora.conformance.artifact_storage import ArtifactStorageConformanceInputs
    from kora.conformance.artifact_type import ArtifactTypeConformanceInputs
    from kora.conformance.authentication import AuthenticationConformanceInputs
    from kora.conformance.harness import HarnessConformanceInputs
    from kora.conformance.policy import PolicyConformanceInputs
    from kora.conformance.runtime import RuntimeConformanceInputs
    from kora.conformance.secrets import SecretsConformanceInputs
    from kora.conformance.vcs import VcsConformanceInputs

ProtocolExerciseBundle: TypeAlias = Union[
    "AuthenticationConformanceInputs",
    "RuntimeConformanceInputs",
    "SecretsConformanceInputs",
    "PolicyConformanceInputs",
    "HarnessConformanceInputs",
    "VcsConformanceInputs",
    "ArtifactStorageConformanceInputs",
    "ArtifactTypeConformanceInputs",
]


class BridgeReason(StrEnum):
    ADAPTER_INVALID = "adapter_invalid"
    PREPARED_INVALID = "prepared_invalid"
    ARGUMENTS_INVALID = "arguments_invalid"
    BUNDLE_INVALID = "bundle_invalid"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    INVOCATION_CLOSED = "invocation_closed"
    RECEIVER_REPEATED = "receiver_repeated"


class ConformanceBridgeError(ValueError):
    def __init__(self, reason: BridgeReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class ConstructorArguments:
    args: tuple[object, ...] = field(default=(), repr=False)
    kwargs: Mapping[str, object] = field(default_factory=dict[str, object], repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))


class PreparedConformance(Protocol):
    @property
    def exercises(self) -> ProtocolExerciseBundle: ...

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments: ...

    async def cleanup(self, instance: object | None) -> None: ...


class ConformanceAdapter(Protocol):
    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance: ...


def bundle_type(protocol: ProtocolName) -> type:
    match protocol:
        case ProtocolName.AUTHENTICATION:
            from kora.conformance.authentication import AuthenticationConformanceInputs

            return AuthenticationConformanceInputs
        case ProtocolName.RUNTIME:
            from kora.conformance.runtime import RuntimeConformanceInputs

            return RuntimeConformanceInputs
        case ProtocolName.SECRETS:
            from kora.conformance.secrets import SecretsConformanceInputs

            return SecretsConformanceInputs
        case ProtocolName.POLICY:
            from kora.conformance.policy import PolicyConformanceInputs

            return PolicyConformanceInputs
        case ProtocolName.HARNESS:
            from kora.conformance.harness import HarnessConformanceInputs

            return HarnessConformanceInputs
        case ProtocolName.VCS:
            from kora.conformance.vcs import VcsConformanceInputs

            return VcsConformanceInputs
        case ProtocolName.ARTIFACT_STORAGE:
            from kora.conformance.artifact_storage import (
                ArtifactStorageConformanceInputs,
            )

            return ArtifactStorageConformanceInputs
        case ProtocolName.ARTIFACT_TYPE:
            from kora.conformance.artifact_type import ArtifactTypeConformanceInputs

            return ArtifactTypeConformanceInputs
        case _:
            raise ConformanceBridgeError(BridgeReason.PROTOCOL_UNSUPPORTED)


def require_method(value: object, name: str, reason: BridgeReason) -> None:
    if not callable(getattr(value, name, None)):
        raise ConformanceBridgeError(reason)


def require_arguments(value: object) -> ConstructorArguments:
    if not isinstance(value, ConstructorArguments):
        raise ConformanceBridgeError(BridgeReason.ARGUMENTS_INVALID)
    return value


async def close_storage(receiver: object) -> None:
    require_method(receiver, "aclose", BridgeReason.PREPARED_INVALID)
    await cast("ArtifactStorage", receiver).aclose()


def cleanup_finished(completed: asyncio.Future[None]) -> None:
    completed.set_result(None)


async def finish_cleanup(
    close: Callable[[], Coroutine[object, object, None]], original: BaseException | None
) -> None:
    task = asyncio.create_task(close())
    completed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    task.add_done_callback(lambda _: cleanup_finished(completed))
    interrupted = False
    while not completed.done():
        try:
            await asyncio.shield(completed)
        except asyncio.CancelledError:
            interrupted = True
    failure = asyncio.CancelledError() if task.cancelled() else task.exception()
    if original is not None and failure is not None:
        raise original from failure
    if original is None and interrupted:
        raise asyncio.CancelledError from failure
    if failure is not None:
        raise failure


class PreparedInvocation:
    def __init__(
        self,
        protocol: ProtocolName,
        implementation: Callable[..., object],
        inputs: ConformanceInputs,
        prepared: PreparedConformance,
    ) -> None:
        self._protocol = protocol
        self._implementation = implementation
        self._inputs = inputs
        self._prepared = prepared
        self._instance: object | None = None
        self._auxiliaries: list[object] = []
        self._active = True
        self._exercises: ProtocolExerciseBundle | None = None

    @property
    def instance(self) -> object:
        return self._instance

    @property
    def configuration(self) -> JsonObject:
        return self._inputs.configuration

    @property
    def exercises(self) -> ProtocolExerciseBundle:
        if self._exercises is None:
            raise ConformanceBridgeError(BridgeReason.BUNDLE_INVALID)
        return self._exercises

    def _construct(self) -> object:
        arguments = require_arguments(
            self._prepared.bind_configuration(self._inputs.configuration)
        )
        return self._implementation(*arguments.args, **arguments.kwargs)

    def initialize(self) -> None:
        require_method(
            self._prepared, "bind_configuration", BridgeReason.PREPARED_INVALID
        )
        exercises = self._prepared.exercises
        if not isinstance(exercises, bundle_type(self._protocol)):
            raise ConformanceBridgeError(BridgeReason.BUNDLE_INVALID)
        self._exercises = exercises
        self._instance = self._construct()
        self._inputs.require_matching_schema(
            getattr(self._instance, "config_schema", None)
        )

    def reconstruct(self) -> ArtifactStorage:
        if not self._active:
            raise ConformanceBridgeError(BridgeReason.INVOCATION_CLOSED)
        if self._protocol is not ProtocolName.ARTIFACT_STORAGE:
            raise ConformanceBridgeError(BridgeReason.PROTOCOL_UNSUPPORTED)
        receiver = self._construct()
        if receiver is self._instance or any(
            receiver is old for old in self._auxiliaries
        ):
            raise ConformanceBridgeError(BridgeReason.RECEIVER_REPEATED)
        self._auxiliaries.append(receiver)
        self._inputs.require_matching_schema(getattr(receiver, "config_schema", None))
        return cast("ArtifactStorage", receiver)

    async def _close(self) -> None:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self._prepared.cleanup, self._instance)
            for receiver in self._auxiliaries:
                stack.push_async_callback(close_storage, receiver)

    async def finish(self, original: BaseException | None) -> None:
        if not self._active:
            raise ConformanceBridgeError(BridgeReason.INVOCATION_CLOSED)
        self._active = False
        await finish_cleanup(self._close, original)


class ConformanceSelection:
    def __init__(
        self,
        metadata: ProviderMetadata,
        configuration: bytes | None = None,
        fixture_data: bytes | None = None,
    ) -> None:
        self._implementation = metadata.implementation
        self._protocol = metadata.protocol
        self._inputs = ConformanceInputs(
            metadata.config_schema, configuration, fixture_data
        )
        bundle_type(self._protocol)

    @asynccontextmanager
    async def prepare(
        self, adapter: object, lifetime: ConformanceLifetime
    ) -> AsyncGenerator[PreparedInvocation]:
        require_method(adapter, "prepare", BridgeReason.ADAPTER_INVALID)
        prepared = await cast("ConformanceAdapter", adapter).prepare(
            self._inputs.configuration, self._inputs.fixture_data, lifetime
        )
        require_method(prepared, "cleanup", BridgeReason.PREPARED_INVALID)
        invocation = PreparedInvocation(
            self._protocol, self._implementation, self._inputs, prepared
        )
        completed = False
        try:
            invocation.initialize()
            yield invocation
            completed = True
        finally:
            await invocation.finish(None if completed else sys.exception())
