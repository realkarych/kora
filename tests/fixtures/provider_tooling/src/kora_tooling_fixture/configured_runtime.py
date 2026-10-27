from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Final, Literal, NoReturn

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.conformance.runtime import RuntimeConformanceInputs
from kora.ir import VcsRef
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import (
    DispatcherQuiescence,
    NormalCompletionQuiescence,
    TerminationReceipt,
)
from kora.protocols.history import (
    GrantHistoryPage,
    GrantHistoryProgress,
    GrantHistorySnapshot,
    RecoveryPage,
    RecoveryProgress,
    RecoveryRead,
)
from kora.protocols.identity import GrantBinding, Handle
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.runtime import (
    AdoptedSession,
    Liveness,
    PublicationAuthorization,
    RawEvent,
    Runtime,
    RuntimeCapabilities,
    RuntimeStatus,
    TransferItem,
    Workload,
)
from kora.protocols.vcs import (
    WorkspaceTarget,
)

CALL_REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "conformance.test_provider.call_refused",
        CONFORMANCE_COMPONENT,
        Level.WARNING,
        "a test runtime or VCS provider refused a call",
        {"reason": FieldKind.STRING},
    )
)


def unsupported() -> NoReturn:
    raise KoraProtocolError("unsupported", "the test provider has no state", False, {})


class ConfiguredRuntime:
    name: str = "test"
    config_schema: JsonObject = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self, value: str) -> None:
        self.execution_domain_id = value

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            supports_resume=False,
            portable_handles=False,
            enforces_network_policy=False,
            isolates_tool_secrets=False,
            credential_proxy=False,
            protected_execution=False,
            plan_executors=frozenset({"runner"}),
            max_concurrent=None,
            default_stall_after=timedelta(minutes=15),
        )

    async def workspace_target(self, attempt_id: str) -> WorkspaceTarget:
        if attempt_id.startswith("kora-conformance"):
            unsupported()
        return WorkspaceTarget(
            root=f"/fixture/{self.execution_domain_id}/{attempt_id}", host_path=None
        )

    async def submit(self, workload: Workload) -> Handle:
        unsupported()

    async def transfer_in(
        self, handle: Handle, item: TransferItem, chunks: AsyncIterator[bytes]
    ) -> None:
        unsupported()

    async def start(self, handle: Handle) -> None:
        unsupported()

    async def lookup(self, attempt_id: str) -> Handle | None:
        return None

    async def adopt(self, handle: Handle) -> AdoptedSession:
        unsupported()

    async def status(self, handle: Handle) -> RuntimeStatus:
        unsupported()

    def events(self, handle: Handle, from_offset: int) -> AsyncIterator[RawEvent]:
        unsupported()

    async def heartbeat(self, handle: Handle) -> Liveness:
        unsupported()

    def transfer_out(self, handle: Handle, name: str) -> AsyncIterator[bytes]:
        unsupported()

    def read_log(
        self, handle: Handle, which: Literal["stdout", "stderr"], from_offset: int
    ) -> AsyncIterator[bytes]:
        unsupported()

    def read_transcript(self, handle: Handle) -> AsyncIterator[bytes]:
        unsupported()

    async def finalize(
        self, handle: Handle, authorization: PublicationAuthorization
    ) -> VcsRef:
        unsupported()

    async def cancel(self, handle: Handle, grace: timedelta) -> None:
        unsupported()

    async def grant_history_status(
        self,
        attempt_id: str,
        current_grant: GrantBinding,
        snapshot: GrantHistorySnapshot,
    ) -> GrantHistoryProgress:
        unsupported()

    async def accept_grant_history(
        self, attempt_id: str, current_grant: GrantBinding, page: GrantHistoryPage
    ) -> GrantHistoryProgress:
        unsupported()

    async def recovery(
        self, attempt_id: str, current_grant: GrantBinding, request: RecoveryRead
    ) -> RecoveryProgress | RecoveryPage:
        unsupported()

    async def quiescence(self, handle: Handle) -> NormalCompletionQuiescence:
        unsupported()

    async def record_dispatcher_quiescence(
        self,
        attempt_id: str,
        acknowledgement: DispatcherQuiescence,
        grant: GrantBinding,
    ) -> None:
        unsupported()

    async def retire(self, attempt_id: str, grace: timedelta) -> TerminationReceipt:
        unsupported()

    async def release(self, handle: Handle) -> None:
        unsupported()


async def failure(
    provider: Runtime, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    context.logger.emit(CALL_REFUSED, reason="unsupported")
    return await provider.workspace_target(marker)


async def effect(
    provider: Runtime, config: JsonObject, context: ProtocolContext
) -> None:
    target = await provider.workspace_target("configured")
    assert target.root == f"/fixture/{config['value']}/configured"


class Adapter:
    exercises: RuntimeConformanceInputs = RuntimeConformanceInputs(failure, effect)

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        return ConstructorArguments((config["value"],))

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None or isinstance(instance, ConfiguredRuntime)


def configured() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.RUNTIME,
        "configured",
        ConfiguredRuntime,
        ConfiguredRuntime.config_schema,
        "1",
    )
