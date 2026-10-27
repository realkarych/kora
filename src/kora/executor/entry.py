import asyncio
import os
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, TypeVar

from pydantic import Field, StrictBytes, ValidationError

from kora.executor import PHASE_FINISHED
from kora.executor.authorization import (
    PinnedPublication,
    check_authorization,
    require_supported,
)
from kora.executor.captures import Captures
from kora.executor.digests import (
    destination_digest,
    operation_id,
    plan_digest,
    prepared_digest,
)
from kora.executor.failures import (
    ExecutorError,
    Phase,
    invalid,
    misconfigured,
    unavailable,
)
from kora.executor.files import (
    opened,
    private_directory,
    remove_private_directory,
    scratch_directory,
    write_new_file,
    write_plan_files,
)
from kora.executor.guard import PhaseGuard
from kora.executor.launcher import StepLauncher
from kora.executor.protected import FuelBounds, check_protected, compiled_globs
from kora.executor.scan import InspectionSources, ScanBounds, ScanVerdict, scan
from kora.executor.seal import (
    EXPORT_DIRECTORY,
    SEALED_FORMAT,
    ExportResult,
    SealBounds,
    seal_sync,
    verify_sync,
)
from kora.executor.steps import Areas, StepContext, redacted, run_steps, selected_tools
from kora.ir import PrincipalRef, VcsRef
from kora.ir.globs import PathGlob
from kora.ir.json import JsonObject
from kora.ir.node import ErrorClass
from kora.ir.scalars import Digest
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import GrantBinding, Identifier
from kora.protocols.runtime import PublicationAuthorization
from kora.protocols.secret_value import SecretValue
from kora.protocols.vcs import (
    CheckoutPlan,
    CheckoutRequest,
    LocationText,
    PlanFile,
    PreparedPublication,
    PublicationInput,
    PublishPlan,
    PublishRequest,
    RelativePath,
    WorkspaceRef,
)

PREPARED_DIGEST_FILE: Final[str] = "prepared_digest"
MAX_REVISION_BYTES: Final[int] = 1_048_576
SEAL_BOUND_NS: Final[int] = 3600 * NANOSECONDS_PER_SECOND
ResultT = TypeVar("ResultT")


class CheckoutResult(BoundaryModel):
    commit: Identifier
    branch: Identifier | None
    base_commit: Identifier
    changed_paths: tuple[RelativePath, ...]
    plan_digest: Digest


class PreparedRevision(BoundaryModel):
    commit: Identifier
    commit_bytes: Annotated[StrictBytes, Field(max_length=MAX_REVISION_BYTES)]
    base_commit: Identifier
    location: LocationText
    plan_digest: Digest
    input_digest: Digest
    prepared_digest: Digest


class PreparedResult(BoundaryModel):
    publication: PreparedPublication
    revision: PreparedRevision
    verdict: ScanVerdict
    pinned: PinnedPublication
    destination_pin: Identifier


class PublishOutcome(BoundaryModel):
    state: Literal["pushed", "equal", "conflict", "unknown"]
    operation_id: Identifier
    ref: VcsRef | None
    principal: PrincipalRef


class RemoteState(StrEnum):
    ABSENT = "absent"
    EQUAL = "equal"
    DIFFERENT = "different"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class PublishPermit:
    operation_id: str


@dataclass(frozen=True)
class Destination:
    provider: str
    remote: str
    pin: str
    transport: JsonObject


class Destinations(Protocol):
    def destination(self, repo: str) -> Destination: ...


class RevisionSink(Protocol):
    async def keep(self, revision: PreparedRevision) -> None: ...


@dataclass(frozen=True)
class ExecutorSettings:
    seal: SealBounds = field(default_factory=SealBounds)
    scan: ScanBounds = field(default_factory=ScanBounds)
    fuel: FuelBounds = field(default_factory=FuelBounds)


async def off_loop(work: Callable[[threading.Event], ResultT]) -> ResultT:
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(work, cancelled)
    finally:
        cancelled.set()


def control_files(plan: PublishPlan) -> tuple[PlanFile, ...]:
    return tuple(item for item in plan.files if item.area == "control")


def publication_files(plan: PublishPlan) -> tuple[PlanFile, ...]:
    return tuple(item for item in plan.files if item.area == "publication")


def signalled(error: ExecutorError) -> bool:
    position = error.position
    return position is not None and (position.exit_code or 0) < 0


def require_area(path: Path | None, phase: Phase) -> Path:
    if path is None:
        misconfigured("area_missing", phase)
    return path


def empty_candidate(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir() or path.is_symlink() or any(path.iterdir()):
            misconfigured("candidate_root_not_empty", Phase.CHECKOUT)


def requested_base(request: CheckoutRequest, commit: str) -> str:
    if isinstance(request.rev, VcsRef):
        return request.rev.base_commit
    return commit


def workspace_values(workspace: WorkspaceRef) -> dict[str, str]:
    values = {"commit": workspace.commit, "base_commit": workspace.base_commit}
    if workspace.branch is not None:
        values["branch"] = workspace.branch
    return values


def vcs_ref(prepared: PreparedResult) -> VcsRef:
    publication = prepared.publication
    return VcsRef.model_validate(
        {
            "repo": publication.repo,
            "branch": publication.branch,
            "commit": publication.commit,
            "base_commit": publication.base_commit,
            "changed_paths": list(publication.changed_paths),
            "diff_stat": publication.diff_stat,
            "pushed_by": prepared.pinned.binding.principal.model_dump(),
        }
    )


def revealed(secrets: Sequence[SecretValue]) -> tuple[str, ...]:
    return tuple(secret.reveal() for secret in secrets)


class Executor:
    def __init__(
        self,
        tools: Mapping[str, str],
        launcher: StepLauncher,
        destinations: Destinations,
        sources: InspectionSources,
        heads: "ReadsHeads",
        settings: ExecutorSettings | None = None,
    ) -> None:
        self.tools = dict(tools)
        self.launcher = launcher
        self.destinations = destinations
        self.sources = sources
        self.heads = heads
        self.settings = settings or ExecutorSettings()

    def context(
        self,
        phase: Phase,
        areas: Areas,
        values: Mapping[str, str],
        required: Sequence[str],
        guard: PhaseGuard,
        ctx: ProtocolContext,
        redactions: tuple[str, ...] = (),
    ) -> StepContext:
        return StepContext(
            phase=phase,
            areas=areas,
            values=values,
            tools=selected_tools(required, self.tools, phase),
            launcher=self.launcher,
            guard=guard,
            logger=ctx.logger,
            redactions=redactions,
        )

    async def prepare_workspace(
        self,
        plan: CheckoutPlan,
        request: CheckoutRequest,
        areas: Areas,
        protected_paths: tuple[str, ...],
        publishing: bool,
        ctx: ProtocolContext,
    ) -> CheckoutResult:
        guard = PhaseGuard.for_deadline(ctx.clock, ctx.deadline)
        guard.check(Phase.CHECKOUT)
        if plan.credentials:
            misconfigured("checkout_credentials_refused", Phase.CHECKOUT)
        if publishing and isinstance(request.rev, VcsRef):
            misconfigured("workspace_base_differs", Phase.CHECKOUT)
        globs = compiled_globs(protected_paths, Phase.CHECKOUT)
        candidate = require_area(areas.candidate, Phase.CHECKOUT)
        control_root = require_area(areas.control, Phase.CHECKOUT)
        empty_candidate(candidate)
        with scratch_directory(control_root, "checkout") as control:
            write_plan_files(plan.files, control, None, Phase.CHECKOUT)
            context = self.context(
                Phase.CHECKOUT,
                Areas(candidate, control, None),
                {"candidate_root": str(candidate), "control_root": str(control)},
                plan.required_tools,
                guard,
                ctx,
            )
            captures = await run_steps(plan.steps, context)
        if captures.commit is None:
            invalid("capture_missing", Phase.CHECKOUT)
        changed = captures.changed_paths or ()
        await check_protected(
            globs, changed, self.settings.fuel, Phase.CHECKOUT, ctx.logger, guard
        )
        result = CheckoutResult(
            commit=captures.commit,
            branch=captures.branch,
            base_commit=requested_base(request, captures.commit),
            changed_paths=changed,
            plan_digest=plan_digest(plan),
        )
        ctx.logger.emit(PHASE_FINISHED, phase=Phase.CHECKOUT.value, outcome="ready")
        return result

    async def export(
        self,
        plan: PublishPlan,
        workspace: WorkspaceRef,
        areas: Areas,
        ctx: ProtocolContext,
    ) -> ExportResult:
        guard = PhaseGuard.for_deadline(ctx.clock, ctx.deadline)
        guard.check(Phase.EXPORT)
        candidate = require_area(areas.candidate, Phase.EXPORT)
        values = {"candidate_root": str(candidate), **workspace_values(workspace)}
        context = self.context(
            Phase.EXPORT,
            Areas(candidate, None, None),
            values,
            plan.required_tools,
            guard,
            ctx,
        )
        await run_steps(plan.export, context)
        ctx.logger.emit(PHASE_FINISHED, phase=Phase.EXPORT.value, outcome="exported")
        return ExportResult(SEALED_FORMAT, EXPORT_DIRECTORY)

    async def seal(
        self,
        exported: ExportResult,
        workspace: WorkspaceRef,
        areas: Areas,
        ctx: ProtocolContext,
    ) -> PublicationInput:
        guard = PhaseGuard.for_deadline(ctx.clock, ctx.deadline)
        guard.check(Phase.SEAL)
        if exported.format != SEALED_FORMAT or exported.directory != EXPORT_DIRECTORY:
            misconfigured("sealed_format_unsupported", Phase.SEAL)
        candidate = require_area(areas.candidate, Phase.SEAL)
        publication = require_area(areas.publication, Phase.SEAL)
        bounds = self.settings.seal
        sealed = await guard.within(
            SEAL_BOUND_NS,
            Phase.SEAL,
            lambda: off_loop(
                lambda cancelled: seal_sync(
                    workspace.attempt_id, candidate, publication, bounds, cancelled
                )
            ),
        )
        ctx.logger.emit(PHASE_FINISHED, phase=Phase.SEAL.value, outcome="sealed")
        return sealed

    async def verify_sealed(
        self, sealed: PublicationInput, areas: Areas
    ) -> PublicationInput:
        publication = require_area(areas.publication, Phase.SEAL)
        return await off_loop(
            lambda cancelled: verify_sync(sealed, publication, cancelled)
        )

    async def prepare_and_inspect(
        self,
        plan: PublishPlan,
        sealed: PublicationInput,
        workspace: WorkspaceRef,
        request: PublishRequest,
        pinned: PinnedPublication,
        areas: Areas,
        secrets: Sequence[SecretValue],
        protected_paths: tuple[str, ...],
        sink: RevisionSink,
        ctx: ProtocolContext,
        persisted: PreparedRevision | None = None,
    ) -> PreparedResult:
        guard = PhaseGuard.for_deadline(ctx.clock, ctx.deadline)
        guard.check(Phase.PREPARE)
        require_supported(pinned.binding, Phase.PREPARE)
        if workspace.base_commit != workspace.commit:
            misconfigured("workspace_base_differs", Phase.PREPARE)
        if not pinned.attempt_id == workspace.attempt_id == request.attempt_id:
            misconfigured("workload_conflict", Phase.PREPARE)
        if len(secrets) > self.settings.scan.max_secrets:
            misconfigured("scanner_secrets_exceeded", Phase.SCAN)
        globs = compiled_globs(protected_paths, Phase.INSPECT)
        digest = plan_digest(plan)
        if persisted is not None and (
            persisted.plan_digest != digest or persisted.input_digest != sealed.digest
        ):
            misconfigured("workload_conflict", Phase.PREPARE)
        destination = self.destination(request.repo, Phase.PREPARE)
        publication_root = require_area(areas.publication, Phase.PREPARE)
        control_root = require_area(areas.control, Phase.PREPARE)
        await self.verify_sealed(sealed, areas)
        texts = revealed(secrets)
        with scratch_directory(control_root, "publication") as control:
            write_plan_files(control_files(plan), control, None, Phase.PREPARE)
            revision = persisted or await self.prepared_revision(
                plan, sealed, workspace, publication_root, control, digest, guard, ctx
            )
            if persisted is None:
                await sink.keep(revision)
            return await self.inspected(
                plan,
                revision,
                workspace,
                request,
                pinned,
                destination,
                Path(revision.location),
                control,
                sealed,
                texts,
                globs,
                guard,
                ctx,
            )

    def destination(self, repo: str, phase: Phase) -> Destination:
        try:
            return self.destinations.destination(repo)
        except KoraProtocolError as error:
            if error.code == "vcs_destination_changed":
                unavailable("destination_changed", phase)
            if error.retryable:
                unavailable("destination_unreachable", phase)
            misconfigured("destination_unknown", phase)

    async def prepared_revision(
        self,
        plan: PublishPlan,
        sealed: PublicationInput,
        workspace: WorkspaceRef,
        publication_root: Path,
        control: Path,
        digest: str,
        guard: PhaseGuard,
        ctx: ProtocolContext,
    ) -> PreparedRevision:
        work = private_directory(publication_root, "prepared")
        try:
            return await self.prepared_in(
                plan, sealed, workspace, work, control, digest, guard, ctx
            )
        except ExecutorError as error:
            if error.code != "step_outcome_unknown":
                await asyncio.to_thread(remove_private_directory, work)
            raise
        except asyncio.CancelledError:
            await asyncio.to_thread(remove_private_directory, work)
            raise

    async def prepared_in(
        self,
        plan: PublishPlan,
        sealed: PublicationInput,
        workspace: WorkspaceRef,
        work: Path,
        control: Path,
        digest: str,
        guard: PhaseGuard,
        ctx: ProtocolContext,
    ) -> PreparedRevision:
        write_plan_files(publication_files(plan), None, work, Phase.PREPARE)
        values = {
            "publication_root": str(work),
            "control_root": str(control),
            "input_location": sealed.location,
            **workspace_values(workspace),
        }
        context = self.context(
            Phase.PREPARE,
            Areas(None, control, work),
            values,
            plan.required_tools,
            guard,
            ctx,
        )
        captures = await run_steps(plan.prepare, context, len(plan.export))
        if captures.commit is None:
            invalid("capture_missing", Phase.PREPARE)
        commit = captures.commit
        async with self.sources.open(str(work)) as source:
            commit_bytes = await self.guarded(
                source.revision_bytes(commit, MAX_REVISION_BYTES), Phase.PREPARE
            )
        revision_digest = prepared_digest(
            commit, commit_bytes, workspace.base_commit, sealed.digest, digest
        )
        revision = PreparedRevision(
            commit=commit,
            commit_bytes=commit_bytes,
            base_commit=workspace.base_commit,
            location=str(work),
            plan_digest=digest,
            input_digest=sealed.digest,
            prepared_digest=revision_digest,
        )
        ctx.logger.emit(PHASE_FINISHED, phase=Phase.PREPARE.value, outcome="prepared")
        return revision

    async def guarded(self, work: Awaitable[ResultT], phase: Phase) -> ResultT:
        try:
            return await work
        except KoraProtocolError:
            unavailable("inspection_source_unavailable", phase)

    async def inspected(
        self,
        plan: PublishPlan,
        revision: PreparedRevision,
        workspace: WorkspaceRef,
        request: PublishRequest,
        pinned: PinnedPublication,
        destination: Destination,
        work: Path,
        control: Path,
        sealed: PublicationInput,
        secrets: tuple[str, ...],
        globs: tuple[PathGlob, ...],
        guard: PhaseGuard,
        ctx: ProtocolContext,
    ) -> PreparedResult:
        if not work.is_dir():
            unavailable("prepared_state_missing", Phase.INSPECT)
        replace_digest_file(work, revision.prepared_digest)
        values = {
            "publication_root": str(work),
            "control_root": str(control),
            "input_location": sealed.location,
            "base_commit": revision.base_commit,
            "commit": revision.commit,
        }
        if workspace.branch is not None:
            values["branch"] = workspace.branch
        context = self.context(
            Phase.INSPECT,
            Areas(None, control, work),
            values,
            plan.required_tools,
            guard,
            ctx,
            secrets,
        )
        captures = await run_steps(
            plan.inspect, context, len(plan.export) + len(plan.prepare)
        )
        manifest = captures.inspection
        if manifest is None:
            invalid("capture_missing", Phase.INSPECT)
        async with self.sources.open(str(work)) as source:
            verdict = await self.guarded(
                scan(
                    manifest,
                    revision.prepared_digest,
                    revision.commit,
                    revision.base_commit,
                    source,
                    secrets,
                    self.settings.scan,
                    guard,
                    ctx.logger,
                    captures.changed_paths or (),
                ),
                Phase.SCAN,
            )
        changed = captures.changed_paths or ()
        await check_protected(
            globs,
            changed,
            self.settings.fuel,
            Phase.INSPECT,
            ctx.logger,
            guard,
        )
        if revision.commit == revision.base_commit and not changed:
            invalid("empty_change", Phase.INSPECT)
        result = self.result(
            revision, request, pinned, destination, captures, changed, verdict, secrets
        )
        ctx.logger.emit(PHASE_FINISHED, phase=Phase.INSPECT.value, outcome="inspected")
        return result

    def result(
        self,
        revision: PreparedRevision,
        request: PublishRequest,
        pinned: PinnedPublication,
        destination: Destination,
        captures: Captures,
        changed: tuple[str, ...],
        verdict: ScanVerdict,
        secrets: tuple[str, ...],
    ) -> PreparedResult:
        target = destination_digest(
            destination.provider,
            destination.remote,
            request.repo,
            request.branch,
            revision.base_commit,
            destination.transport,
        )
        try:
            publication = PreparedPublication(
                attempt_id=pinned.attempt_id,
                operation_id=operation_id(pinned.attempt_id, pinned.output),
                input_digest=revision.input_digest,
                prepared_digest=revision.prepared_digest,
                inspection_digest=verdict.inspection_digest,
                repo=request.repo,
                branch=request.branch,
                commit=revision.commit,
                base_commit=revision.base_commit,
                destination_digest=target,
                changed_paths=changed,
                diff_stat=redacted(captures.diff_stat or "", secrets),
            )
        except ValidationError:
            invalid("prepared_publication_invalid", Phase.INSPECT)
        return PreparedResult(
            publication=publication,
            revision=revision,
            verdict=verdict,
            pinned=pinned,
            destination_pin=destination.pin,
        )

    def destination_unchanged(self, prepared: PreparedResult, phase: Phase) -> bool:
        destination = self.destination(prepared.publication.repo, phase)
        target = destination_digest(
            destination.provider,
            destination.remote,
            prepared.publication.repo,
            prepared.publication.branch,
            prepared.publication.base_commit,
            destination.transport,
        )
        return (
            destination.pin == prepared.destination_pin
            and target == prepared.publication.destination_digest
        )

    async def publish(
        self,
        prepared: PreparedResult,
        plan: PublishPlan,
        authorization: PublicationAuthorization,
        grant: GrantBinding,
        permit: PublishPermit | None,
        ctx: ProtocolContext,
        recorded: PublishOutcome | None = None,
    ) -> PublishOutcome:
        guard = PhaseGuard.for_deadline(ctx.clock, ctx.deadline)
        if plan_digest(plan) != prepared.revision.plan_digest:
            misconfigured("workload_conflict", Phase.PUBLISH)
        check_authorization(
            authorization, prepared.pinned, prepared.publication, grant, guard.due()
        )
        operation = prepared.publication.operation_id
        if permit is None or permit.operation_id != operation:
            return recorded or self.outcome(prepared, "unknown")
        if not self.destination_unchanged(prepared, Phase.PUBLISH):
            unavailable("destination_changed", Phase.PUBLISH)
        work = Path(prepared.revision.location)
        if not work.is_dir():
            unavailable("prepared_state_missing", Phase.PUBLISH)
        publication_root = work.parent
        with scratch_directory(publication_root, "publish") as control:
            write_plan_files(control_files(plan), control, None, Phase.PUBLISH)
            values = {
                "publication_root": str(work),
                "control_root": str(control),
                "commit": prepared.revision.commit,
                "base_commit": prepared.revision.base_commit,
            }
            context = self.context(
                Phase.PUBLISH,
                Areas(None, control, work),
                values,
                plan.required_tools,
                guard,
                ctx,
            )
            first = len(plan.export) + len(plan.prepare) + len(plan.inspect)
            try:
                await run_steps(plan.publish, context, first)
            except ExecutorError as error:
                if error.failure_class is ErrorClass.TIMEOUT or (
                    error.code == "step_outcome_unknown" or signalled(error)
                ):
                    return self.outcome(prepared, "unknown")
                if error.code != "step_failed":
                    raise
                return await self.settled_after_failure(prepared, ctx)
        if not self.destination_still(prepared):
            return self.outcome(prepared, "unknown")
        ctx.logger.emit(PHASE_FINISHED, phase=Phase.PUBLISH.value, outcome="pushed")
        return self.outcome(prepared, "pushed")

    def destination_still(self, prepared: PreparedResult) -> bool:
        try:
            return self.destination_unchanged(prepared, Phase.PUBLISH)
        except ExecutorError:
            return False

    async def settled_after_failure(
        self, prepared: PreparedResult, ctx: ProtocolContext
    ) -> PublishOutcome:
        state = await self.remote_state(prepared, ctx)
        if state is RemoteState.EQUAL:
            return self.outcome(prepared, "equal")
        if state is RemoteState.DIFFERENT:
            return self.outcome(prepared, "conflict")
        return self.outcome(prepared, "unknown")

    def outcome(
        self,
        prepared: PreparedResult,
        state: Literal["pushed", "equal", "conflict", "unknown"],
    ) -> PublishOutcome:
        settled = state in ("pushed", "equal")
        return PublishOutcome(
            state=state,
            operation_id=prepared.publication.operation_id,
            ref=vcs_ref(prepared) if settled else None,
            principal=prepared.pinned.binding.principal,
        )

    async def reconcile(
        self, prepared: PreparedResult, ctx: ProtocolContext
    ) -> RemoteState:
        try:
            unchanged = self.destination_unchanged(prepared, Phase.RECONCILE)
        except ExecutorError as error:
            if error.code != "destination_unreachable":
                raise
            return RemoteState.UNREACHABLE
        if not unchanged:
            unavailable("destination_changed", Phase.RECONCILE)
        state = await self.remote_state(prepared, ctx)
        ctx.logger.emit(
            PHASE_FINISHED, phase=Phase.RECONCILE.value, outcome=state.value
        )
        return state

    async def remote_state(
        self, prepared: PreparedResult, ctx: ProtocolContext
    ) -> RemoteState:
        try:
            head = await self.heads.head_sha(vcs_ref(prepared), ctx)
        except KoraProtocolError as error:
            if error.code == "vcs_ref_missing":
                return RemoteState.ABSENT
            return RemoteState.UNREACHABLE
        if head == prepared.revision.commit:
            return RemoteState.EQUAL
        return RemoteState.DIFFERENT


class ReadsHeads(Protocol):
    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str: ...


def replace_digest_file(work: Path, digest: str) -> None:
    with opened(work) as directory:
        try:
            os.unlink(PREPARED_DIGEST_FILE, dir_fd=directory)
        except FileNotFoundError:
            pass
        write_new_file(directory, PREPARED_DIGEST_FILE, digest.encode("ascii"), 0o600)
