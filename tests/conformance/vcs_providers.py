import tempfile
from collections.abc import Callable
from pathlib import Path

from kora.conformance import vcs as vcs_cases
from kora.conformance.capture import Capture
from kora.ir import PrincipalRef, VcsRef
from kora.plugins.vcs.git import GitVcs
from kora.protocols.common import ProtocolContext
from kora.protocols.vcs import (
    CheckoutPlan,
    CheckoutRequest,
    CredentialRequest,
    IssuedCredential,
    PlanStep,
    PublishPlan,
    PublishRequest,
)
from tests.plugins.vcs.git_support import World, make_world

AUTHOR_ENVIRONMENT: dict[str, str] = {
    "GIT_AUTHOR_NAME": "Broken",
    "GIT_AUTHOR_EMAIL": "broken@example.invalid",
    "GIT_COMMITTER_NAME": "Broken",
    "GIT_COMMITTER_EMAIL": "broken@example.invalid",
}


def appended(plan: CheckoutPlan, steps: tuple[PlanStep, ...]) -> CheckoutPlan:
    return plan.model_copy(update={"steps": (*plan.steps, *steps)})


class MovingCheckoutVcs(GitVcs):
    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan:
        plan = await super().checkout_plan(request, ctx)
        capture = plan.steps[-2]
        commit = capture.model_copy(
            update={
                "argv": ("git", "commit", "-q", "--allow-empty", "-m", "moved"),
                "env": {**capture.env, **AUTHOR_ENVIRONMENT},
                "capture": None,
            }
        )
        return appended(plan, (commit, capture))


class LeakingControlVcs(GitVcs):
    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan:
        plan = await super().checkout_plan(request, ctx)
        last = plan.steps[-1]
        leak = last.model_copy(
            update={
                "argv": ("git", "config", "core.hooksPath", "{control_root}/hooks"),
                "capture": None,
            }
        )
        return appended(plan, (leak,))


class UnbuiltTreeVcs(GitVcs):
    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan:
        plan = await super().publish_plan(request, ctx)
        build = plan.prepare[3]
        skipped = build.model_copy(
            update={
                "argv": (
                    "git",
                    "--git-dir=repo",
                    "update-ref",
                    "refs/kora/prepared",
                    "refs/kora/head",
                )
            }
        )
        prepare = (*plan.prepare[:3], skipped, *plan.prepare[4:])
        return plan.model_copy(update={"prepare": prepare})


class StaleHeadVcs(GitVcs):
    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        head = await super().head_sha(ref, ctx)
        return "0" * len(head)


class ShortHeadVcs(GitVcs):
    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        head = await super().head_sha(ref, ctx)
        return head[:12]


class ForeignPrincipalVcs(GitVcs):
    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        issued = await super().acquire_credential(request, ctx)
        principal = PrincipalRef.model_validate(
            {**issued.principal.model_dump(), "stable_id": "someone-else"}
        )
        return issued.model_copy(update={"principal": principal})


class PermissiveBindingVcs(GitVcs):
    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        configured = self.remotes[request.repo].configuration
        fixed = request.model_copy(
            update={"binding_revision": configured.binding_revision}
        )
        return await super().acquire_credential(fixed, ctx)


class ApiAllowingVcs(GitVcs):
    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        if request.purpose == "api":
            request = request.model_copy(
                update={"purpose": "checkout", "attempt_id": "attempt"}
            )
        return await super().acquire_credential(request, ctx)


def with_world(
    capture: Capture, provider_type: type[GitVcs], case: Callable[[World], None]
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        case(make_world(Path(directory), capture, provider_type=provider_type))


def checkout_case(world: World) -> None:
    vcs_cases.test_vcs_checkout_captures_requested_base(
        world.provider, world.remote_fixture(), world.bench(), world.context()
    )


def private_case(world: World) -> None:
    vcs_cases.test_vcs_private_files_stay_outside_candidate(
        world.provider, world.remote_fixture(), world.bench(), world.context()
    )


def sealed_case(world: World) -> None:
    vcs_cases.test_vcs_sealed_transfer_fixes_inspected_revision(
        world.provider,
        world.remote_fixture(),
        world.bench(),
        world.context(),
        world.binding(),
    )


def repeated_case(world: World) -> None:
    vcs_cases.test_vcs_repeated_finalize_returns_one_ref(
        world.provider,
        world.remote_fixture(),
        world.bench(),
        world.context(),
        world.binding(),
    )


def head_case(world: World) -> None:
    vcs_cases.test_vcs_head_sha(
        world.provider,
        world.remote_fixture(),
        world.bench(),
        world.context(),
        world.binding(),
    )


def local_credential_case(world: World) -> None:
    vcs_cases.test_vcs_local_credential_carries_no_secret(
        world.provider, world.remote_fixture(), world.context(), world.binding()
    )


def mismatch_case(world: World) -> None:
    vcs_cases.test_vcs_credential_binding_mismatch_is_refused(
        world.provider, world.remote_fixture(), world.context(), world.binding()
    )


def api_case(world: World) -> None:
    vcs_cases.test_vcs_api_credential_is_refused(
        world.provider, world.remote_fixture(), world.context(), world.binding()
    )


def moving_checkout(capture: Capture) -> None:
    with_world(capture, MovingCheckoutVcs, checkout_case)


def leaking_control(capture: Capture) -> None:
    with_world(capture, LeakingControlVcs, private_case)


def unbuilt_tree(capture: Capture) -> None:
    with_world(capture, UnbuiltTreeVcs, sealed_case)


def stale_head(capture: Capture) -> None:
    with_world(capture, StaleHeadVcs, repeated_case)


def short_head(capture: Capture) -> None:
    with_world(capture, ShortHeadVcs, head_case)


def foreign_principal(capture: Capture) -> None:
    with_world(capture, ForeignPrincipalVcs, local_credential_case)


def permissive_binding(capture: Capture) -> None:
    with_world(capture, PermissiveBindingVcs, mismatch_case)


def api_allowing(capture: Capture) -> None:
    with_world(capture, ApiAllowingVcs, api_case)


VCS_BROKEN_CASES: tuple[tuple[Callable[[Capture], None], str], ...] = (
    (moving_checkout, "checkout captures the requested revision"),
    (leaking_control, "the candidate configuration names no control path"),
    (unbuilt_tree, "the sealed change prepares"),
    (stale_head, "the destination holds the prepared revision"),
    (short_head, "head_sha returns the published object id"),
    (foreign_principal, "the principal is the declared local principal"),
    (permissive_binding, "a mismatched binding is refused"),
    (api_allowing, "an api credential is refused in slice 1"),
)
