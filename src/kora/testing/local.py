import asyncio
import hashlib
import os
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeVar

from kora.executor.digests import object_digest
from kora.executor.launcher import run_sync
from kora.executor.steps import resolve_tools
from kora.ir.config import NodeLimits
from kora.ir.cost import PriceRevision
from kora.ir.json import JsonObject, JsonObjectField
from kora.plugins.harness.fake import CallScope, FakeHarness
from kora.plugins.runtime.local.bound_checkout import BoundCheckout
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.plugins.runtime.local.spawn import SYSTEM_PATH
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.plugins.vcs.git.environment import git_environment, trusted_options
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import duration_ns
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import AgentExecution, SessionInputs, prompt_digest
from kora.protocols.identity import Handle
from kora.protocols.runtime import CheckoutSource, TransferItem, Workload
from kora.protocols.vcs import CheckoutRequest, WorkspaceRef
from kora.runner.identity import NativeIdentityReader
from kora.runner.registry import refusal
from kora.testing.script import FakeScript
from kora.testing.seed_checkout import (
    FIXTURE_TIMEOUT_NS,
    SeedCheckout,
    SeedSourceObserver,
)

T = TypeVar("T")
TESTING_PROMPT: Final[str] = "run the script"
TESTING_PRICE_TABLE: Final[PriceRevision] = PriceRevision(
    id="testing-prices",
    namespace_id=None,
    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
    version=1,
    normalization_version=1,
    prices={},
)


def testing_runner_command() -> tuple[str, ...]:
    return (sys.executable, "-I", "-m", "kora.testing.runner")


async def script_chunks(buffer: bytes) -> AsyncGenerator[bytes]:
    for offset in range(0, len(buffer), 65_536):
        yield buffer[offset : offset + 65_536]


def script_item(buffer: bytes) -> TransferItem:
    return TransferItem(
        kind="script",
        name="script.json",
        size=len(buffer),
        sha256="sha256:" + hashlib.sha256(buffer).hexdigest(),
    )


async def unless_busy(operation: Callable[[], Awaitable[T]]) -> T:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while True:
        try:
            return await operation()
        except KoraProtocolError as error:
            if error.code != "workload_busy" or loop.time() >= deadline:
                raise
        ready = loop.create_future()
        loop.call_soon(ready.set_result, None)
        await ready


class TestingLocal:
    def __init__(
        self,
        runtime: LocalRuntime,
        provider: GitVcs,
        root: Path,
        remote: Path,
        seed_checkout: SeedCheckout,
        ctx: ProtocolContext,
    ) -> None:
        self.runtime = runtime
        self.provider = provider
        self.root = root
        self.remote = remote
        self.commit = seed_checkout.object_ids[0]
        self.ctx = ctx
        self.seed_checkout = seed_checkout
        self._seed_domain = runtime.execution_domain_id
        self._seed_provider = provider
        self._seed_tools = provider.git_tools
        self._seed_remote = provider.remotes["local"]
        self._seed_binding = self._seed_remote.configuration
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(
        cls,
        root: Path,
        sockets: Path,
        ctx: ProtocolContext,
    ) -> "TestingLocal":
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with ExitStack() as held:
            original_root = BoundCheckout.capture(root)
            held.callback(original_root.close)
            original_remote = original_root.create_child(root / "repository.git")
            held.callback(original_remote.close)
            local = await cls._open_owned(original_root, original_remote, sockets, ctx)
            held.pop_all()
            return local

    @classmethod
    async def _open_owned(
        cls,
        original_root: BoundCheckout,
        original_remote: BoundCheckout,
        sockets: Path,
        ctx: ProtocolContext,
    ) -> "TestingLocal":
        root = original_root.path
        remote = original_remote.path
        home = root / "home"
        home.mkdir(mode=0o700)
        hooks = root / "hooks"
        hooks.mkdir(mode=0o700)
        seed = root / "seed"
        seed.mkdir(mode=0o700)
        git = resolve_tools(("git",), SYSTEM_PATH.split(":"))["git"]
        environment = git_environment(git, str(home), trusted_options(str(hooks), ()))
        environment.update(
            {
                "GIT_AUTHOR_NAME": "kora",
                "GIT_AUTHOR_EMAIL": "kora@example.invalid",
                "GIT_COMMITTER_NAME": "kora",
                "GIT_COMMITTER_EMAIL": "kora@example.invalid",
            }
        )

        def command(*arguments: str) -> str:
            result = run_sync((git, *arguments), seed, environment, FIXTURE_TIMEOUT_NS)
            if result.exit_code != 0:
                raise KoraProtocolError(
                    "testing_repository_failed",
                    "repository preparation failed",
                    False,
                    {},
                )
            return result.stdout.decode("utf-8").strip()

        command("init", "--bare", "--template=", str(remote))
        provider = GitVcs(
            GitConfiguration.model_validate(
                {
                    "tool_path": SYSTEM_PATH.split(":"),
                    "identity": {"name": "kora", "email": "kora@example.invalid"},
                    "remotes": {
                        "local": {
                            "path": str(remote),
                            "binding": "testing-local",
                            "binding_revision": 1,
                            "credential_mode": "local_credential_free",
                            "principal": {
                                "provider_instance": "testing",
                                "stable_id": "testing",
                                "display_name": "testing",
                            },
                        }
                    },
                }
            )
        )
        command("init", "--template=", str(seed))
        (seed / "seed.txt").write_text("testing composition\n")
        (seed / "script.json").write_text(
            '{"end":"cli_exit_without_result","on_interrupt":"ignore","steps":[]}'
        )
        command("add", "seed.txt", "script.json")
        command("commit", "-m", "Seed testing repository")
        command("push", str(remote), "HEAD:refs/heads/kora")
        seed_checkout = SeedCheckout(
            original_root,
            original_remote,
            git,
            command("rev-parse", "--show-object-format"),
            tuple(
                command(
                    "rev-parse",
                    "HEAD",
                    "HEAD^{tree}",
                    "HEAD:seed.txt",
                    "HEAD:script.json",
                ).splitlines()
            ),
            ctx.clock,
        )
        (root / "state").mkdir(mode=0o700)
        runtime = await LocalRuntime.open(
            LocalRuntimeConfiguration(state_root=str(root / "state")),
            ctx,
            environment={},
            home=lambda: home,
            socket_directory=sockets,
            runner=testing_runner_command(),
        )
        return cls(runtime, provider, root, remote, seed_checkout, ctx)

    def _seed_binding_current(self) -> bool:
        if self._close_task is not None:
            return False
        source = self.seed_checkout.source
        remote = self.provider.remotes.get("local")
        identity = source.anchor.identity
        return (
            self.runtime.execution_domain_id == self._seed_domain
            and self.provider is self._seed_provider
            and self.provider.git_tools == self._seed_tools
            and self._seed_tools.git == self.seed_checkout.git
            and remote is not None
            and remote is self._seed_remote
            and remote.configuration == self._seed_binding
            and remote.path == str(self.remote) == str(source.path)
            and (remote.pin.device, remote.pin.inode)
            == (identity.device, identity.inode)
            and self.commit == self.seed_checkout.object_ids[0]
        )

    @asynccontextmanager
    async def exact_seed_checkout(
        self, candidate: Path, *, observe_source: SeedSourceObserver | None = None
    ) -> AsyncGenerator[BoundCheckout]:
        async with self.seed_checkout.prepare(
            candidate, self._seed_binding_current, observe_source
        ) as checkout:
            yield checkout

    async def workload(
        self,
        attempt_id: str,
        source: FakeScript | None = None,
        harness: str = "claude",
    ) -> Workload:
        if self._close_task is not None:
            raise refusal("runtime_closed")
        workspace = await self.runtime.workspace_target(attempt_id, self.ctx)
        checkout = await self.provider.checkout_plan(
            CheckoutRequest(
                repo="local",
                rev=self.commit,
                root=workspace.root,
                attempt_id=attempt_id,
                branch="kora-attempt",
            ),
            self.ctx,
        )
        identity = NativeIdentityReader().read(os.getpid()).process
        if identity is None:
            raise KoraProtocolError(
                "testing_identity_unavailable",
                "process identity unavailable",
                False,
                {},
            )
        execution = AgentExecution.model_validate(
            {
                "harness": harness,
                "node_path": "graph.node",
                "runtime_attempt": 1,
                "policy_layers": {
                    "namespace": {"allow": [], "deny": []},
                    "node": {"allow": [], "deny": []},
                },
                "output_format": False,
            }
        )
        request: JsonObject = execution.model_dump(mode="json")
        config: JsonObject = {
            "harness": harness,
            "prompt": TESTING_PROMPT,
            "workspace": {"repo": "local"},
        }
        spec = None
        if source is not None:
            scope = CallScope(
                harness=harness,
                workspace=WorkspaceRef(
                    attempt_id=attempt_id,
                    root=workspace.root,
                    repo="local",
                    commit=self.commit,
                    base_commit=self.commit,
                    branch="kora-attempt",
                ),
                node_path=execution.node_path,
                layers=execution.layers(),
                protected_paths=(),
                output_format=False,
            )
            spec = FakeHarness(source.listed(), scope, self.ctx).build_session_spec(
                config,
                workspace,
                None,
                inputs=SessionInputs(
                    prompt=TESTING_PROMPT,
                    prompt_digest=prompt_digest(TESTING_PROMPT),
                    price_table_id=TESTING_PRICE_TABLE.id,
                    price_table=TESTING_PRICE_TABLE,
                    limits=NodeLimits(),
                ),
            )
        else:
            request = {"node": "graph.command"}
            config = {
                "cmd": [
                    sys.executable,
                    "-I",
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('seed.txt').read_text() == 'testing composition\\n'",
                ],
                "workspace": {"repo": "local"},
            }
        return Workload(
            attempt_id=attempt_id,
            execution_domain_id=self.runtime.execution_domain_id,
            dispatcher_id="testing-dispatcher",
            dispatcher_process=identity.identity,
            execution_request=JsonObjectField(request),
            execution_request_digest=object_digest(request),
            kind="agent" if source is not None else "command",
            resolved_config=JsonObjectField(config),
            workspace=workspace,
            checkout=checkout,
            publish=None,
            plan_executor="runner",
            env={},
            secret_refs={},
            harness_credentials={},
            artifact_inputs={},
            resources=(),
            limits=JsonObjectField(
                {
                    "stall_after_us": str(
                        duration_ns(self.runtime.capabilities().default_stall_after)
                        // 1000
                    )
                }
                if source is not None
                else {}
            ),
            deadline=None,
            session_spec=spec,
            checkout_request=CheckoutSource(
                repo="local",
                rev=self.commit,
                pinned=None,
                branch="kora-attempt",
                protected_paths=(),
            ),
        )

    async def run_agent(self, attempt_id: str, source: FakeScript) -> Handle:
        buffer = source.serialise()
        work = await self.workload(attempt_id, source)
        handle = await self.runtime.submit(work, self.ctx)
        await self.runtime.transfer_in(
            handle, script_item(buffer), script_chunks(buffer), self.ctx
        )
        await unless_busy(lambda: self.runtime.start(handle, self.ctx))
        return handle

    async def run_command(self, attempt_id: str) -> Handle:
        work = await self.workload(attempt_id)
        handle = await self.runtime.submit(work, self.ctx)
        await unless_busy(lambda: self.runtime.start(handle, self.ctx))
        return handle

    async def close(self) -> None:
        task = self._close_task
        if task is None or (
            task.done() and (task.cancelled() or task.exception() is not None)
        ):
            task = asyncio.create_task(self._close_owned())
            self._close_task = task
        await asyncio.shield(task)

    async def _close_owned(self) -> None:
        try:
            await self.runtime.close()
        finally:
            await self.seed_checkout.close()
