import asyncio
from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError

from kora.conformance import vcs as checks
from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    check_capabilities_are_frozen_and_consistent,
    check_config_schema_is_an_object_schema,
    check_failure_is_a_bounded_protocol_error,
    check_provider_logs_through_its_context,
    utc_now,
)
from kora.executor.launcher import StepLauncher
from kora.ir import VcsRef
from kora.ir.json import JsonObject, parse_json_object
from kora.observability import LoggingSettings
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import (
    CheckoutPlan,
    CheckoutRequest,
    CredentialRequest,
    IssuedCredential,
    PublishPlan,
    PublishRequest,
    Vcs,
)
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.plugins.vcs.git_support import REPO, World, configuration, make_world

MARKER: str = "vcs-shared-input-marker"


class LoopBoundGit(GitVcs):
    def __init__(
        self, configuration: GitConfiguration, launcher: StepLauncher | None = None
    ) -> None:
        super().__init__(configuration, launcher)
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[asyncio.Future[None] | None] = asyncio.Queue()
        self.worker = asyncio.create_task(self.serve())
        self.contexts: list[ProtocolContext] = []
        self.credentials: list[CredentialRequest] = []
        self.cleanup_loop: asyncio.AbstractEventLoop | None = None

    async def serve(self) -> None:
        while (completion := await self.queue.get()) is not None:
            if not completion.done():
                completion.set_result(None)

    async def advance(self, context: ProtocolContext) -> None:
        assert asyncio.get_running_loop() is self.loop
        completion: asyncio.Future[None] = self.loop.create_future()
        self.queue.put_nowait(completion)
        await completion
        self.contexts.append(context)

    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan:
        await self.advance(ctx)
        return await super().checkout_plan(request, ctx)

    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan:
        await self.advance(ctx)
        return await super().publish_plan(request, ctx)

    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        await self.advance(ctx)
        return await super().head_sha(ref, ctx)

    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        await self.advance(ctx)
        self.credentials.append(request)
        return await super().acquire_credential(request, ctx)

    async def close(self) -> None:
        self.queue.put_nowait(None)
        await self.worker
        self.cleanup_loop = asyncio.get_running_loop()


class EchoingGit(LoopBoundGit):
    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        await self.advance(ctx)
        raise KoraProtocolError("vcs_binding_mismatch", request.repo, False, {})


def configured_request(document: JsonObject) -> CredentialRequest:
    config = GitConfiguration.model_validate(document)
    remote = config.remotes[REPO]
    return CredentialRequest(
        binding=remote.binding,
        binding_revision=remote.binding_revision,
        credential_mode=remote.credential_mode,
        repo=REPO,
        purpose="checkout",
        attempt_id="configured-attempt",
        operation_id=None,
        prepared_digest=None,
        destination_digest=None,
    )


async def failing_input(
    provider: Vcs, document: JsonObject, context: ProtocolContext, marker: str
) -> object:
    request = configured_request(document).model_copy(update={"repo": f"/srv/{marker}"})
    return await provider.acquire_credential(request, context)


async def configuration_effect(
    provider: Vcs, document: JsonObject, context: ProtocolContext
) -> None:
    config = GitConfiguration.model_validate(document)

    issued = await provider.acquire_credential(configured_request(document), context)

    assert issued.principal.stable_id == config.remotes[REPO].principal.stable_id
    assert issued.binding == config.remotes[REPO].binding
    assert issued.binding_revision == config.remotes[REPO].binding_revision


async def build_bench(
    world: World, provider: Vcs, document: JsonObject, context: ProtocolContext
) -> checks.VcsBench:
    assert provider is world.provider
    assert isinstance(provider, LoopBoundGit)
    await provider.advance(context)
    assert configured_request(document).repo == world.remote_fixture().repo
    bench = world.bench()
    assert bench.executor.destinations is provider
    assert bench.executor.sources is provider
    assert bench.executor.heads is provider
    return bench


async def exercise_vcs(root: Path, captures: list[Capture]) -> LoopBoundGit:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "vcs-shared", utc_now
    ) as capture:
        captures.append(capture)
        world = make_world(root, capture, provider_type=LoopBoundGit)
        provider = world.provider
        assert isinstance(provider, LoopBoundGit)
        try:
            document = parse_json_object(configuration(world.remote, root / "gone.git"))
            context = replace(world.context(), namespace_id="vcs-shared")
            remote = world.remote_fixture()
            inputs = checks.VcsConformanceInputs(
                failure=failing_input,
                remote=remote,
                head_remote=replace(
                    remote, publish_branch=remote.publish_branch + "-head"
                ),
                binding=world.binding(),
                bench=partial(build_bench, world),
                configuration_effect=configuration_effect,
            )
            bench = await inputs.bench(provider, document, context)
            check_capabilities_are_frozen_and_consistent(provider)
            check_config_schema_is_an_object_schema(provider)
            await checks.check_vcs_checkout_captures_requested_base(
                provider, inputs.remote, bench, context
            )
            await checks.check_vcs_private_files_stay_outside_candidate(
                provider, inputs.remote, bench, context
            )
            await checks.check_vcs_sealed_transfer_fixes_inspected_revision(
                provider, inputs.remote, bench, context, inputs.binding
            )
            await checks.check_vcs_repeated_finalize_returns_one_ref(
                provider, inputs.remote, bench, context, inputs.binding
            )
            await checks.check_vcs_head_sha(
                provider, inputs.head_remote, bench, context, inputs.binding
            )
            await checks.check_vcs_local_credential_carries_no_secret(
                provider, inputs.remote, context, inputs.binding
            )
            await checks.check_vcs_credential_binding_mismatch_is_refused(
                provider, inputs.remote, context, inputs.binding
            )
            await checks.check_vcs_api_credential_is_refused(
                provider, inputs.remote, context, inputs.binding
            )
            await inputs.configuration_effect(provider, document, context)
            failure = partial(inputs.failure, provider, document)
            await check_failure_is_a_bounded_protocol_error(failure, context, MARKER)
            await check_provider_logs_through_its_context(
                failure, capture, context, MARKER
            )
            assert all(item is context for item in provider.contexts)
            assert [request.repo for request in provider.credentials[-2:]] == [
                f"/srv/{MARKER}"
            ] * 2
            assert world.remote_head(inputs.remote.publish_branch) is not None
            assert world.remote_head(inputs.head_remote.publish_branch) is not None
            assert (
                world.remote_head(inputs.head_remote.publish_branch + "-absent") is None
            )
        finally:
            await provider.close()
    return provider


def test_shared_vcs_checks_use_one_caller_loop_and_exact_inputs() -> None:
    captures: list[Capture] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with asyncio.Runner() as runner:
            expected_loop = runner.get_loop()

            provider = runner.run(exercise_vcs(root, captures))

            assert provider.loop is provider.cleanup_loop is expected_loop
            assert provider.worker.done()
            assert provider.queue.empty()
            assert not asyncio.all_tasks(expected_loop)
        assert root.exists()
    assert not root.exists()
    assert len(captures) == 1
    assert_capture_released(captures[0])


async def exercise_ending(
    root: Path, ending: str, captures: list[Capture], providers: list[LoopBoundGit]
) -> None:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "vcs-ending", utc_now
    ) as capture:
        captures.append(capture)
        world = make_world(
            root,
            capture,
            provider_type=EchoingGit if ending == "failure" else LoopBoundGit,
        )
        provider = world.provider
        assert isinstance(provider, LoopBoundGit)
        providers.append(provider)
        try:
            document = parse_json_object(configuration(world.remote, root / "gone.git"))
            async with asyncio.timeout(0 if ending == "cancel" else 5):
                if ending == "unexpected":
                    document.pop("remotes")
                    await configuration_effect(provider, document, world.context())
                else:
                    await check_failure_is_a_bounded_protocol_error(
                        partial(failing_input, provider, document),
                        world.context(),
                        MARKER,
                    )
        finally:
            await provider.close()


@pytest.mark.parametrize("ending", ["success", "failure", "cancel", "unexpected"])
def test_shared_vcs_checks_release_every_ending(ending: str) -> None:
    captures: list[Capture] = []
    providers: list[LoopBoundGit] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with asyncio.Runner() as runner:
            operation = exercise_ending(root, ending, captures, providers)

            if ending == "failure":
                with pytest.raises(
                    pytest.fail.Exception, match="the message carries no input"
                ):
                    runner.run(operation)
            elif ending == "cancel":
                with pytest.raises(TimeoutError):
                    runner.run(operation)
            elif ending == "unexpected":
                with pytest.raises(ValidationError):
                    runner.run(operation)
            else:
                runner.run(operation)

            assert not asyncio.all_tasks(runner.get_loop())
    assert not root.exists()
    assert len(captures) == len(providers) == 1
    assert providers[0].worker.done()
    assert providers[0].loop is providers[0].cleanup_loop
    assert_capture_released(captures[0])


class FinalizingGit(GitVcs):
    def __init__(
        self, configuration: GitConfiguration, launcher: StepLauncher | None = None
    ) -> None:
        super().__init__(configuration, launcher)
        self.pending: list[asyncio.Task[None]] = []

    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        issued = await super().acquire_credential(request, ctx)
        started = asyncio.Event()

        async def finish_later() -> None:
            started.set()
            try:
                await asyncio.Future[None]()
            finally:
                object.__setattr__(
                    issued, "binding_revision", issued.binding_revision + 1
                )

        self.pending.append(asyncio.create_task(finish_later()))
        await started.wait()
        return issued


def test_sync_vcs_check_observes_owned_loop_finalization(tmp_path: Path) -> None:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "vcs-finalization", utc_now
    ) as capture:
        world = make_world(tmp_path, capture, provider_type=FinalizingGit)
        provider = world.provider
        assert isinstance(provider, FinalizingGit)

        with pytest.raises(
            pytest.fail.Exception, match="the returned tuple matches the request"
        ):
            checks.test_vcs_local_credential_carries_no_secret(
                provider, world.remote_fixture(), world.context(), world.binding()
            )

        assert len(provider.pending) == 2
        assert all(task.done() for task in provider.pending)
    assert_capture_released(capture)
