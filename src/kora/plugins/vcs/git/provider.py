import tempfile
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Final

from kora.executor.entry import Destinations, ReadsHeads
from kora.executor.launcher import (
    Ending,
    LaunchSpec,
    ProcessLauncher,
    StepLauncher,
)
from kora.executor.scan import InspectionSource, InspectionSources
from kora.ir import VcsRef
from kora.ir.json import JsonObject, parse_json_object
from kora.plugins.vcs.git.environment import git_environment, trusted_options
from kora.plugins.vcs.git.plans import (
    CommitIdentity,
    PlanSettings,
    checkout_plan,
    is_branch,
    is_commit_message,
    is_full_ref,
    is_object_id,
    is_publish_branch,
    publish_plan,
)
from kora.plugins.vcs.git.remotes import check_bare
from kora.plugins.vcs.git.source import GitInspectionSources
from kora.plugins.vcs.git.tools import GitTools, ToolRefused, resolve_git_tools
from kora.plugins.vcs.local import (
    DisplayText,
    LocalConfiguration,
    LocalEvents,
    LocalVcs,
    Remote,
    configuration_refused,
    local_events,
    located_remote,
    refusal,
)
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.vcs import (
    CheckoutPlan,
    CheckoutRequest,
    PublishPlan,
    PublishRequest,
)

VERSION: Final[str] = "1"
PROVIDER_NAME: Final[str] = "git"
HEAD_TIMEOUT_NS: Final[int] = 30 * NANOSECONDS_PER_SECOND
HEAD_OUTPUT: Final[int] = 65_536
MISSING_REF_EXIT: Final[int] = 2
EVENTS: Final[LocalEvents] = local_events("vcs.git", PROVIDER_NAME)


class IdentityConfiguration(BoundaryModel):
    name: DisplayText
    email: DisplayText


class GitConfiguration(LocalConfiguration):
    identity: IdentityConfiguration


CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    GitConfiguration.model_json_schema()
)


class GitVcs(LocalVcs, Destinations, InspectionSources, ReadsHeads):
    name: str = PROVIDER_NAME
    config_schema: JsonObject = CONFIG_SCHEMA

    def __init__(
        self,
        configuration: GitConfiguration,
        launcher: StepLauncher | None = None,
    ) -> None:
        self.launcher = launcher or ProcessLauncher()
        identity = configuration.identity
        with tempfile.TemporaryDirectory(prefix="kora-git-") as home:
            try:
                self.git_tools: GitTools = resolve_git_tools(
                    configuration.tool_path, Path(home)
                )
            except ToolRefused as refused:
                configuration_refused(refused.rule)
            git = self.git_tools.git
            remotes = {
                repo: located_remote(
                    repo,
                    remote,
                    lambda path: check_bare(path, git, Path(home)),
                    (identity.name, identity.email),
                )
                for repo, remote in configuration.remotes.items()
            }
        super().__init__(PROVIDER_NAME, EVENTS, remotes)
        self.identity = CommitIdentity(identity.name, identity.email)
        self.sources = GitInspectionSources(self.git_tools, self.launcher)

    @property
    def tools(self) -> Mapping[str, str]:
        return self.git_tools.mapping()

    def settings(self, remote: Remote) -> PlanSettings:
        return PlanSettings(self.git_tools, remote.path, self.identity)

    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan:
        remote = self.remote(request.repo, ctx)
        rev = request.rev
        if isinstance(rev, VcsRef):
            valid = is_object_id(rev.commit) and is_object_id(rev.base_commit)
        else:
            valid = is_object_id(rev) or is_full_ref(rev)
        if not valid or not is_branch(request.branch):
            self.refuse(ctx, "vcs_request_invalid")
        plan = checkout_plan(self.settings(remote), rev, request.branch)
        ctx.logger.emit(self.events.planned, kind="checkout")
        return plan

    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan:
        remote = self.remote(request.repo, ctx)
        if not is_publish_branch(request.branch) or not is_commit_message(
            request.commit_message
        ):
            self.refuse(ctx, "vcs_request_invalid")
        plan = publish_plan(
            self.settings(remote),
            request.branch,
            request.commit_message,
            ctx.clock.now(),
        )
        ctx.logger.emit(self.events.planned, kind="publish")
        return plan

    def open(
        self, prepared_location: str
    ) -> AbstractAsyncContextManager[InspectionSource]:
        return self.sources.open(prepared_location)

    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        remote = self.remote(ref.repo, ctx)
        if not is_branch(ref.branch):
            self.refuse(ctx, "vcs_request_invalid")
        name = f"refs/heads/{ref.branch}"
        home = tempfile.mkdtemp(prefix="kora-git-head-")
        try:
            outcome = await self.launcher.run(
                LaunchSpec(
                    argv=(
                        self.git_tools.git,
                        "ls-remote",
                        "--exit-code",
                        "--end-of-options",
                        remote.path,
                        name,
                    ),
                    cwd=Path(home),
                    env=git_environment(
                        self.git_tools.git, home, trusted_options(f"{home}/hooks", ())
                    ),
                    stdin=None,
                    timeout_ns=HEAD_TIMEOUT_NS,
                    stdout_limit=HEAD_OUTPUT,
                    stderr_limit=4096,
                )
            )
        finally:
            Path(home).rmdir()
        self.require_same(remote, ctx)
        if outcome.ending is Ending.EXITED and outcome.exit_code == MISSING_REF_EXIT:
            ctx.logger.emit(self.events.head_read, outcome="missing")
            raise refusal("vcs_ref_missing")
        if not outcome.succeeded or outcome.stdout_overflow:
            self.refuse(ctx, "vcs_remote_unreachable")
        return self.parsed_head(outcome.stdout, name, ctx)

    def parsed_head(self, output: bytes, name: str, ctx: ProtocolContext) -> str:
        matches = [
            line.split(b"\t", 1)[0].decode("ascii", "replace")
            for line in output.splitlines()
            if line.split(b"\t", 1)[1:] == [name.encode("utf-8")]
        ]
        if len(matches) != 1 or not is_object_id(matches[0]):
            self.refuse(ctx, "vcs_remote_unreachable")
        ctx.logger.emit(self.events.head_read, outcome="found")
        return matches[0]


def create_provider(configuration: JsonObject) -> GitVcs:
    return GitVcs(GitConfiguration.model_validate(configuration))


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.VCS, PROVIDER_NAME, create_provider, CONFIG_SCHEMA, VERSION
    )
