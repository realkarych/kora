from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Final, Literal, NoReturn

from kora.conformance.common import CONFORMANCE_COMPONENT
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
from kora.protocols.runtime import (
    AdoptedSession,
    Liveness,
    PublicationAuthorization,
    RawEvent,
    RuntimeCapabilities,
    RuntimeStatus,
    TransferItem,
    Workload,
)
from kora.protocols.vcs import (
    CheckoutPlan,
    CheckoutRequest,
    CredentialRequest,
    IssuedCredential,
    PublishPlan,
    PublishRequest,
    VcsCapabilities,
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


class ReferenceRuntime:
    name: str = "test"
    config_schema: JsonObject = {"type": "object"}
    execution_domain_id: str = "conformance-domain"

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
        unsupported()

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


class ReferenceVcs:
    name: str = "test"
    config_schema: JsonObject = {"type": "object"}

    def capabilities(self) -> VcsCapabilities:
        return VcsCapabilities(
            change_requests=False,
            review_comments=False,
            ci_status=False,
            head_sha_watch=False,
            reviewer_requests=False,
            plan_executors=frozenset({"runner", "worker_host"}),
        )

    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        ctx.logger.emit(CALL_REFUSED, reason="unsupported")
        unsupported()

    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan:
        unsupported()

    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan:
        unsupported()

    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        unsupported()


async def runtime_exercise(ctx: ProtocolContext, marker: str) -> object:
    ctx.logger.emit(CALL_REFUSED, reason="unsupported")
    return await ReferenceRuntime().workspace_target(marker)


async def vcs_exercise(ctx: ProtocolContext, marker: str) -> object:
    request = CredentialRequest.model_validate(
        {
            "binding": "conformance",
            "binding_revision": 1,
            "credential_mode": "local_credential_free",
            "repo": f"/srv/git/{marker}.git",
            "purpose": "api",
            "attempt_id": None,
            "operation_id": None,
            "prepared_digest": None,
            "destination_digest": None,
        }
    )
    return await ReferenceVcs().acquire_credential(request, ctx)
