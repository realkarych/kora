import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TypeVar

import pytest

from kora.conformance.common import (
    await_protocol_error,
    check,
    fail,
    kora_capture,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.executor.authorization import (
    PinnedPublication,
    TrustedBinding,
    credential_request,
)
from kora.executor.entry import (
    CheckoutResult,
    Executor,
    PreparedResult,
    PreparedRevision,
    PublishOutcome,
    PublishPermit,
    RemoteState,
)
from kora.executor.failures import ExecutorError
from kora.executor.steps import Areas
from kora.ir import VcsRef
from kora.ir.json import JsonObject
from kora.ir.node import ErrorClass
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import GrantBinding
from kora.protocols.runtime import PublicationAuthorization
from kora.protocols.vcs import (
    CheckoutRequest,
    CredentialRequest,
    IssuedCredential,
    PublicationInput,
    PublishPlan,
    PublishRequest,
    Vcs,
    WorkspaceRef,
)

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
    "test_vcs_api_credential_is_refused",
    "test_vcs_checkout_captures_requested_base",
    "test_vcs_credential_binding_mismatch_is_refused",
    "test_vcs_head_sha",
    "test_vcs_local_credential_carries_no_secret",
    "test_vcs_private_files_stay_outside_candidate",
    "test_vcs_repeated_finalize_returns_one_ref",
    "test_vcs_sealed_transfer_fixes_inspected_revision",
]

ATTEMPT: Final[str] = "conformance-attempt"
DOMAIN: Final[str] = "conformance-domain"
DISPATCHER: Final[str] = "conformance-dispatcher"
OUTPUT: Final[str] = "vcs_ref"
SEALED_FILE: Final[str] = "conformance-sealed.txt"
LATE_FILE: Final[str] = "conformance-late.txt"
WORK_BRANCH: Final[str] = "conformance-work"
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class VcsRemote:
    repo: str
    revision: str
    missing_revision: str
    unreachable_repo: str
    publish_branch: str
    commit_message: str


@dataclass(frozen=True)
class VcsBench:
    executor: Executor
    root: Path
    counter: list[int] = field(default_factory=lambda: [0])

    def areas(self) -> Areas:
        self.counter[0] += 1
        base = self.root / f"case-{self.counter[0]}"
        for name in ("control", "publication"):
            (base / name).mkdir(parents=True)
        return Areas(base / "candidate", base / "control", base / "publication")


@dataclass(frozen=True)
class VcsBinding:
    binding: TrustedBinding


@dataclass(frozen=True)
class VcsConformanceInputs:
    failure: Callable[[Vcs, JsonObject, ProtocolContext, str], Awaitable[object]]
    remote: VcsRemote
    head_remote: VcsRemote
    binding: VcsBinding
    bench: Callable[[Vcs, JsonObject, ProtocolContext], Awaitable[VcsBench]]
    configuration_effect: Callable[[Vcs, JsonObject, ProtocolContext], Awaitable[None]]


class Kept:
    def __init__(self) -> None:
        self.revisions: list[PreparedRevision] = []

    async def keep(self, revision: PreparedRevision) -> None:
        self.revisions.append(revision)


@dataclass(frozen=True)
class Checkout:
    request: CheckoutRequest
    result: CheckoutResult
    areas: Areas

    def workspace(self) -> WorkspaceRef:
        return WorkspaceRef(
            attempt_id=self.request.attempt_id,
            root=self.request.root,
            repo=self.request.repo,
            commit=self.result.commit,
            base_commit=self.result.base_commit,
            branch=self.result.branch,
        )


@dataclass(frozen=True)
class Prepared:
    checkout: Checkout
    plan: PublishPlan
    sealed: PublicationInput
    result: PreparedResult


def run(work: Coroutine[object, object, ResultT]) -> ResultT:
    return asyncio.run(work)


async def checked_out(
    provider: Vcs,
    executor: Executor,
    areas: Areas,
    repo: str,
    revision: str,
    ctx: ProtocolContext,
    publishing: bool = True,
) -> Checkout:
    request = CheckoutRequest(
        repo=repo,
        rev=revision,
        root=str(areas.candidate),
        attempt_id=ATTEMPT,
        branch=WORK_BRANCH,
    )
    plan = await provider.checkout_plan(request, ctx)
    result = await executor.prepare_workspace(plan, request, areas, (), publishing, ctx)
    return Checkout(request, result, areas)


def checkout_of(
    provider: Vcs,
    bench: VcsBench,
    remote: VcsRemote,
    ctx: ProtocolContext,
    *,
    repo: str | None = None,
    revision: str | None = None,
) -> Coroutine[object, object, Checkout]:
    return checked_out(
        provider,
        bench.executor,
        bench.areas(),
        remote.repo if repo is None else repo,
        remote.revision if revision is None else revision,
        ctx,
    )


def publish_request(remote: VcsRemote, checkout: Checkout) -> PublishRequest:
    return PublishRequest(
        repo=remote.repo,
        root=checkout.request.root,
        attempt_id=ATTEMPT,
        branch=remote.publish_branch,
        commit_message=remote.commit_message,
    )


def pinned(binding: VcsBinding) -> PinnedPublication:
    return PinnedPublication(
        attempt_id=ATTEMPT,
        execution_domain_id=DOMAIN,
        output=OUTPUT,
        binding=binding.binding,
    )


async def sealed_input(
    provider: Vcs,
    executor: Executor,
    remote: VcsRemote,
    checkout: Checkout,
    ctx: ProtocolContext,
) -> tuple[PublishPlan, PublicationInput]:
    plan = await provider.publish_plan(publish_request(remote, checkout), ctx)
    workspace = checkout.workspace()
    exported = await executor.export(plan, workspace, checkout.areas, ctx)
    sealed = await executor.seal(exported, workspace, checkout.areas, ctx)
    return plan, sealed


async def prepared_from(
    executor: Executor,
    remote: VcsRemote,
    binding: VcsBinding,
    checkout: Checkout,
    plan: PublishPlan,
    sealed: PublicationInput,
    ctx: ProtocolContext,
) -> Prepared:
    result = await executor.prepare_and_inspect(
        plan,
        sealed,
        checkout.workspace(),
        publish_request(remote, checkout),
        pinned(binding),
        checkout.areas,
        (),
        (),
        Kept(),
        ctx,
    )
    return Prepared(checkout, plan, sealed, result)


def grant() -> GrantBinding:
    return GrantBinding(
        dispatcher_id=DISPATCHER,
        worker_id="conformance-worker",
        incarnation_id="conformance-incarnation",
        lease_epoch=1,
        execution_domain_id=DOMAIN,
        process=None,
    )


def authorization(prepared: PreparedResult) -> PublicationAuthorization:
    binding = prepared.pinned.binding
    return PublicationAuthorization(
        attempt_id=prepared.pinned.attempt_id,
        execution_domain_id=prepared.pinned.execution_domain_id,
        dispatcher_id=DISPATCHER,
        operation_id=prepared.publication.operation_id,
        prepared_digest=prepared.publication.prepared_digest,
        destination_digest=prepared.publication.destination_digest,
        binding=binding.binding,
        binding_revision=binding.binding_revision,
        credential_mode=binding.credential_mode,
        principal=binding.principal,
        credential=None,
    )


async def published(
    executor: Executor, prepared: Prepared, ctx: ProtocolContext
) -> PublishOutcome:
    result = prepared.result
    return await executor.publish(
        result,
        prepared.plan,
        authorization(result),
        grant(),
        PublishPermit(result.publication.operation_id),
        ctx,
    )


def write_candidate(checkout: Checkout, name: str, content: str) -> None:
    candidate = checkout.areas.candidate
    if candidate is None:
        fail("the checkout has a candidate root")
    (candidate / name).write_text(content, encoding="utf-8")


def attempted(work: Callable[[], ResultT], message: str) -> ResultT:
    try:
        return work()
    except (ExecutorError, KoraProtocolError):
        fail(message)


async def attempted_async(work: Awaitable[ResultT], message: str) -> ResultT:
    try:
        return await work
    except (ExecutorError, KoraProtocolError):
        fail(message)


async def failure_class_async(work: Awaitable[object]) -> ErrorClass | None:
    try:
        await work
    except ExecutorError as error:
        return error.failure_class
    except KoraProtocolError as error:
        return ErrorClass.INFRASTRUCTURE if error.retryable else ErrorClass.CONFIG_ERROR
    return None


@pytest.fixture
def kora_provider(kora_vcs_provider: Vcs) -> Vcs:
    return kora_vcs_provider


@pytest.fixture
def kora_exercise(kora_vcs_exercise: object) -> object:
    return kora_vcs_exercise


@pytest.mark.kora_conformance
def test_vcs_checkout_captures_requested_base(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
) -> None:
    remote, bench, ctx = kora_vcs_remote, kora_vcs_executor, kora_vcs_context

    checkout = run(checkout_of(kora_vcs_provider, bench, remote, ctx))
    missing = run(
        failure_class_async(
            checkout_of(
                kora_vcs_provider, bench, remote, ctx, revision=remote.missing_revision
            )
        )
    )
    unreachable = run(
        failure_class_async(
            checkout_of(
                kora_vcs_provider, bench, remote, ctx, repo=remote.unreachable_repo
            )
        )
    )

    check_checkout_captures_requested_base(checkout, remote, missing, unreachable)


async def check_vcs_checkout_captures_requested_base(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
) -> None:
    (remote, bench, ctx) = (kora_vcs_remote, kora_vcs_executor, kora_vcs_context)
    checkout = await checkout_of(kora_vcs_provider, bench, remote, ctx)
    missing = await failure_class_async(
        checkout_of(
            kora_vcs_provider, bench, remote, ctx, revision=remote.missing_revision
        )
    )
    unreachable = await failure_class_async(
        checkout_of(kora_vcs_provider, bench, remote, ctx, repo=remote.unreachable_repo)
    )
    check_checkout_captures_requested_base(checkout, remote, missing, unreachable)


def check_checkout_captures_requested_base(
    checkout: Checkout,
    remote: VcsRemote,
    missing: ErrorClass | None,
    unreachable: ErrorClass | None,
) -> None:
    check(
        checkout.result.commit == remote.revision,
        "checkout captures the requested revision",
    )
    check(
        checkout.result.base_commit == remote.revision,
        "a revision checkout is its own base",
    )
    check(missing is ErrorClass.CONFIG_ERROR, "an absent revision is config_error")
    check(
        unreachable is ErrorClass.INFRASTRUCTURE,
        "an unreachable remote is infrastructure",
    )


def control_named(checkout: Checkout) -> bool:
    candidate, control = checkout.areas.candidate, checkout.areas.control
    if candidate is None or control is None:
        return True
    markers = (str(control), str(control.resolve()))
    for path in candidate.rglob("*"):
        if path.is_file() and not path.is_symlink():
            text = path.read_bytes().decode("utf-8", "replace")
            if any(marker in text for marker in markers):
                return True
    return False


@pytest.mark.kora_conformance
def test_vcs_private_files_stay_outside_candidate(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
) -> None:
    remote, bench = kora_vcs_remote, kora_vcs_executor

    checkout = run(checkout_of(kora_vcs_provider, bench, remote, kora_vcs_context))
    check_private_files_stay_outside_candidate(checkout)


async def check_vcs_private_files_stay_outside_candidate(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
) -> None:
    (remote, bench) = (kora_vcs_remote, kora_vcs_executor)
    checkout = await checkout_of(kora_vcs_provider, bench, remote, kora_vcs_context)
    check_private_files_stay_outside_candidate(checkout)


def check_private_files_stay_outside_candidate(checkout: Checkout) -> None:
    control = checkout.areas.control

    check(
        not control_named(checkout),
        "the candidate configuration names no control path",
    )
    check(
        control is not None and not any(control.iterdir()),
        "control files are released when checkout returns",
    )


@pytest.mark.kora_conformance
def test_vcs_sealed_transfer_fixes_inspected_revision(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    remote, bench, ctx = kora_vcs_remote, kora_vcs_executor, kora_vcs_context
    checkout = run(checkout_of(kora_vcs_provider, bench, remote, ctx))
    write_candidate(checkout, SEALED_FILE, "sealed\n")
    plan, sealed = run(
        sealed_input(kora_vcs_provider, bench.executor, remote, checkout, ctx)
    )
    write_candidate(checkout, LATE_FILE, "late\n")

    prepared = attempted(
        lambda: run(
            prepared_from(
                bench.executor, remote, kora_vcs_binding, checkout, plan, sealed, ctx
            )
        ),
        "the sealed change prepares",
    )
    verified = run(bench.executor.verify_sealed(sealed, checkout.areas))
    paths = prepared.result.publication.changed_paths

    check_sealed_transfer_fixes_inspected_revision(paths, sealed, verified)


async def check_vcs_sealed_transfer_fixes_inspected_revision(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    (remote, bench, ctx) = (kora_vcs_remote, kora_vcs_executor, kora_vcs_context)
    checkout = await checkout_of(kora_vcs_provider, bench, remote, ctx)
    write_candidate(checkout, SEALED_FILE, "sealed\n")
    (plan, sealed) = await sealed_input(
        kora_vcs_provider, bench.executor, remote, checkout, ctx
    )
    write_candidate(checkout, LATE_FILE, "late\n")
    prepared = await attempted_async(
        prepared_from(
            bench.executor, remote, kora_vcs_binding, checkout, plan, sealed, ctx
        ),
        "the sealed change prepares",
    )
    verified = await bench.executor.verify_sealed(sealed, checkout.areas)
    paths = prepared.result.publication.changed_paths
    check_sealed_transfer_fixes_inspected_revision(paths, sealed, verified)


def check_sealed_transfer_fixes_inspected_revision(
    paths: tuple[str, ...], sealed: PublicationInput, verified: PublicationInput
) -> None:
    check(SEALED_FILE in paths, "the prepared revision holds the sealed content")
    check(LATE_FILE not in paths, "content written after sealing is not published")
    check(verified.digest == sealed.digest, "the sealed digest is stable")


@pytest.mark.kora_conformance
def test_vcs_repeated_finalize_returns_one_ref(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    remote, bench, ctx = kora_vcs_remote, kora_vcs_executor, kora_vcs_context
    checkout = run(checkout_of(kora_vcs_provider, bench, remote, ctx))
    write_candidate(checkout, SEALED_FILE, "repeated\n")
    plan, sealed = run(
        sealed_input(kora_vcs_provider, bench.executor, remote, checkout, ctx)
    )
    prepared = run(
        prepared_from(
            bench.executor, remote, kora_vcs_binding, checkout, plan, sealed, ctx
        )
    )

    first = run(published(bench.executor, prepared, ctx))
    second = run(published(bench.executor, prepared, ctx))
    state = run(bench.executor.reconcile(prepared.result, ctx))

    check_repeated_finalize_returns_one_ref(first, second, state)


async def check_vcs_repeated_finalize_returns_one_ref(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    (remote, bench, ctx) = (kora_vcs_remote, kora_vcs_executor, kora_vcs_context)
    checkout = await checkout_of(kora_vcs_provider, bench, remote, ctx)
    write_candidate(checkout, SEALED_FILE, "repeated\n")
    (plan, sealed) = await sealed_input(
        kora_vcs_provider, bench.executor, remote, checkout, ctx
    )
    prepared = await prepared_from(
        bench.executor, remote, kora_vcs_binding, checkout, plan, sealed, ctx
    )
    first = await published(bench.executor, prepared, ctx)
    second = await published(bench.executor, prepared, ctx)
    state = await bench.executor.reconcile(prepared.result, ctx)
    check_repeated_finalize_returns_one_ref(first, second, state)


def check_repeated_finalize_returns_one_ref(
    first: PublishOutcome, second: PublishOutcome, state: RemoteState
) -> None:
    check(first.ref is not None, "the first finalize publishes a ref")
    check(first.ref == second.ref, "repeated finalize returns one ref")
    check(state.value == "equal", "the destination holds the prepared revision")


@pytest.mark.kora_conformance
def test_vcs_head_sha(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    remote, bench, ctx = kora_vcs_remote, kora_vcs_executor, kora_vcs_context
    checkout = run(checkout_of(kora_vcs_provider, bench, remote, ctx))
    write_candidate(checkout, SEALED_FILE, "head\n")
    plan, sealed = run(
        sealed_input(kora_vcs_provider, bench.executor, remote, checkout, ctx)
    )
    prepared = run(
        prepared_from(
            bench.executor, remote, kora_vcs_binding, checkout, plan, sealed, ctx
        )
    )
    outcome = run(published(bench.executor, prepared, ctx))
    ref = outcome.ref
    if ref is None:
        fail("the publication returns a ref")
    missing = absent_branch(ref, remote)

    head = run(kora_vcs_provider.head_sha(ref, ctx))
    missing_error = refusal(lambda: run(kora_vcs_provider.head_sha(missing, ctx)))

    check_head_sha(head, ref, missing_error)


async def check_vcs_head_sha(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_executor: VcsBench,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    (remote, bench, ctx) = (kora_vcs_remote, kora_vcs_executor, kora_vcs_context)
    checkout = await checkout_of(kora_vcs_provider, bench, remote, ctx)
    write_candidate(checkout, SEALED_FILE, "head\n")
    (plan, sealed) = await sealed_input(
        kora_vcs_provider, bench.executor, remote, checkout, ctx
    )
    prepared = await prepared_from(
        bench.executor, remote, kora_vcs_binding, checkout, plan, sealed, ctx
    )
    outcome = await published(bench.executor, prepared, ctx)
    ref = outcome.ref
    if ref is None:
        fail("the publication returns a ref")
    missing = absent_branch(ref, remote)
    head = await kora_vcs_provider.head_sha(ref, ctx)
    missing_error = await await_protocol_error(kora_vcs_provider.head_sha(missing, ctx))
    check_head_sha(head, ref, missing_error)


def absent_branch(ref: VcsRef, remote: VcsRemote) -> VcsRef:
    return VcsRef.model_validate(
        {
            **ref.model_dump(mode="json", exclude_unset=True),
            "branch": f"{remote.publish_branch}-absent",
        }
    )


def check_head_sha(
    head: str, ref: VcsRef, missing_error: KoraProtocolError | None
) -> None:
    check(head == ref.commit, "head_sha returns the published object id")
    check(
        len(head) in (40, 64) and head == head.lower(),
        "head_sha returns a full object id",
    )
    check(
        missing_error is not None and missing_error.code == "vcs_ref_missing",
        "a missing ref is vcs_ref_missing",
    )
    check(
        missing_error is not None and not missing_error.retryable,
        "a missing ref is not retryable",
    )


def refusal(work: Callable[[], object]) -> KoraProtocolError | None:
    try:
        work()
    except KoraProtocolError as error:
        return error
    return None


def local_request(
    binding: VcsBinding, remote: VcsRemote, purpose: str
) -> CredentialRequest:
    if purpose == "checkout":
        return credential_request(
            binding.binding, remote.repo, "checkout", ATTEMPT, None
        )
    return CredentialRequest(
        binding=binding.binding.binding,
        binding_revision=binding.binding.binding_revision,
        credential_mode=binding.binding.credential_mode,
        repo=remote.repo,
        purpose="publish",
        attempt_id=ATTEMPT,
        operation_id="conformance-operation",
        prepared_digest="sha256:" + "0" * 64,
        destination_digest="sha256:" + "1" * 64,
    )


def stale_request(binding: VcsBinding, remote: VcsRemote) -> CredentialRequest:
    request = local_request(binding, remote, "checkout")
    return CredentialRequest.model_validate(
        {**request.model_dump(), "binding_revision": request.binding_revision + 1}
    )


def api_request(binding: VcsBinding, remote: VcsRemote) -> CredentialRequest:
    trusted = binding.binding
    return CredentialRequest(
        binding=trusted.binding,
        binding_revision=trusted.binding_revision,
        credential_mode=trusted.credential_mode,
        repo=remote.repo,
        purpose="api",
        attempt_id=None,
        operation_id=None,
        prepared_digest=None,
        destination_digest=None,
    )


@pytest.mark.kora_conformance
def test_vcs_local_credential_carries_no_secret(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    binding = kora_vcs_binding.binding
    if binding.credential_mode != "local_credential_free":
        return
    requests = [
        local_request(kora_vcs_binding, kora_vcs_remote, purpose)
        for purpose in ("checkout", "publish")
    ]

    issued = [
        run(kora_vcs_provider.acquire_credential(request, kora_vcs_context))
        for request in requests
    ]

    check_local_credential_carries_no_secret(issued, binding)


async def check_vcs_local_credential_carries_no_secret(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    binding = kora_vcs_binding.binding
    if binding.credential_mode != "local_credential_free":
        return
    requests = [
        local_request(kora_vcs_binding, kora_vcs_remote, purpose)
        for purpose in ("checkout", "publish")
    ]
    issued = [
        await kora_vcs_provider.acquire_credential(request, kora_vcs_context)
        for request in requests
    ]
    check_local_credential_carries_no_secret(issued, binding)


def check_local_credential_carries_no_secret(
    issued: list[IssuedCredential], binding: TrustedBinding
) -> None:
    for credential in issued:
        check(credential.value is None, "a local credential carries no value")
        check(
            (
                credential.binding,
                credential.binding_revision,
                credential.credential_mode,
            )
            == binding.tuple(),
            "the returned tuple matches the request",
        )
        check(
            credential.principal.identity() == binding.principal.identity(),
            "the principal is the declared local principal",
        )


@pytest.mark.kora_conformance
def test_vcs_credential_binding_mismatch_is_refused(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    stale = stale_request(kora_vcs_binding, kora_vcs_remote)

    error = refusal(
        lambda: run(kora_vcs_provider.acquire_credential(stale, kora_vcs_context))
    )

    check_credential_binding_mismatch_is_refused(error, kora_vcs_remote)


async def check_vcs_credential_binding_mismatch_is_refused(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    stale = stale_request(kora_vcs_binding, kora_vcs_remote)
    error = await await_protocol_error(
        kora_vcs_provider.acquire_credential(stale, kora_vcs_context)
    )
    check_credential_binding_mismatch_is_refused(error, kora_vcs_remote)


def check_credential_binding_mismatch_is_refused(
    error: KoraProtocolError | None, remote: VcsRemote
) -> None:
    check(error is not None, "a mismatched binding is refused")
    check(error is not None and not error.retryable, "the refusal is not retryable")
    check(
        error is not None and remote.repo not in error.message,
        "the refusal carries no input",
    )


@pytest.mark.kora_conformance
def test_vcs_api_credential_is_refused(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    request = api_request(kora_vcs_binding, kora_vcs_remote)

    error = refusal(
        lambda: run(kora_vcs_provider.acquire_credential(request, kora_vcs_context))
    )

    check_api_credential_is_refused(error)


async def check_vcs_api_credential_is_refused(
    kora_vcs_provider: Vcs,
    kora_vcs_remote: VcsRemote,
    kora_vcs_context: ProtocolContext,
    kora_vcs_binding: VcsBinding,
) -> None:
    request = api_request(kora_vcs_binding, kora_vcs_remote)
    error = await await_protocol_error(
        kora_vcs_provider.acquire_credential(request, kora_vcs_context)
    )
    check_api_credential_is_refused(error)


def check_api_credential_is_refused(error: KoraProtocolError | None) -> None:
    check(error is not None, "an api credential is refused in slice 1")
    check(
        error is not None and not error.retryable,
        "the api refusal is not retryable",
    )
