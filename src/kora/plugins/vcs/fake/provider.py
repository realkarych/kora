import os
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from kora.executor.entry import Destinations, ReadsHeads
from kora.executor.scan import InspectionSource, InspectionSources
from kora.executor.seal import EXPORT_DIRECTORY
from kora.executor.steps import AreaName, ToolMissing, resolve_tools
from kora.ir import VcsRef
from kora.ir.json import JsonObject, parse_json_object
from kora.plugins.vcs.fake import store
from kora.plugins.vcs.fake.source import DirInspectionSources
from kora.plugins.vcs.local import (
    LocalConfiguration,
    LocalEvents,
    LocalVcs,
    Remote,
    RemoteRefused,
    configuration_refused,
    limited_argv,
    local_events,
    located_remote,
    needs_prlimit,
    refusal,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.vcs import (
    Capture,
    CheckoutPlan,
    CheckoutRequest,
    PlanFile,
    PlanStep,
    PublishPlan,
    PublishRequest,
    StepFailureClass,
)

PROVIDER_NAME: Final[str] = "fake"
HELPER: Final[str] = store.__file__
REPOSITORY_FILE: Final[str] = "repository.json"
REPOSITORY_ARGUMENT: Final[str] = "{control_root}/" + REPOSITORY_FILE
PUBLISH_PREFIX: Final[str] = "kora/"
SHORT: Final[timedelta] = timedelta(minutes=1)
WORK: Final[timedelta] = timedelta(minutes=30)
EVENTS: Final[LocalEvents] = local_events("vcs.fake", PROVIDER_NAME)
CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    LocalConfiguration.model_json_schema()
)


@dataclass(frozen=True)
class HelperTools:
    python: str
    prlimit: str | None

    def mapping(self) -> dict[str, str]:
        tools = {"python": self.python}
        if self.prlimit is not None:
            tools["prlimit"] = self.prlimit
        return tools


def check_store(path: str) -> None:
    for section in (*store.SECTIONS, "heads"):
        try:
            status = os.stat(Path(path) / section, follow_symlinks=False)
        except OSError:
            raise RemoteRefused("remote_not_a_store") from None
        if not stat.S_ISDIR(status.st_mode):
            raise RemoteRefused("remote_not_a_store")


def is_publish_branch(name: str) -> bool:
    return name.startswith(PUBLISH_PREFIX) and store.is_branch(name)


class Steps:
    def __init__(self, tools: HelperTools) -> None:
        self.tools = tools

    def step(
        self,
        arguments: Sequence[str],
        cwd: AreaName,
        failure: StepFailureClass | None,
        capture: Capture | None = None,
        limited: bool = False,
        timeout: timedelta = WORK,
    ) -> PlanStep:
        full = ("-I", HELPER, *arguments)
        prlimit = self.tools.prlimit if limited else None
        return PlanStep(
            argv=limited_argv(prlimit, "python", self.tools.python, full),
            cwd=cwd,
            env={},
            stdin=None,
            timeout=timeout,
            capture=capture,
            failure_class=failure,
        )

    def control(
        self,
        arguments: Sequence[str],
        failure: StepFailureClass | None,
        capture: Capture | None = None,
    ) -> PlanStep:
        return self.step(arguments, "control", failure, capture, timeout=SHORT)

    def publisher(
        self,
        arguments: Sequence[str],
        failure: StepFailureClass | None,
        capture: Capture | None = None,
        cwd: AreaName = "publication",
    ) -> PlanStep:
        return self.step(arguments, cwd, failure, capture, limited=True)


def checkout_steps(steps: Steps, rev: str | VcsRef, branch: str) -> list[PlanStep]:
    commit = rev.commit if isinstance(rev, VcsRef) else rev
    requested = (commit, rev.base_commit) if isinstance(rev, VcsRef) else (commit,)
    repository = ("--repository", REPOSITORY_ARGUMENT)
    found = [
        steps.control(("repository", *repository), "infrastructure"),
        steps.control(
            (
                "verify",
                *repository,
                *(item for revision in requested for item in ("--revision", revision)),
            ),
            "config_error",
        ),
        steps.step(
            ("materialise", *repository, "--revision", commit),
            "candidate",
            None,
        ),
        steps.control(("show", *repository, "--revision", commit), None, "commit"),
        steps.control(("branch", "--branch", branch), None, "branch"),
    ]
    if isinstance(rev, VcsRef):
        found.append(
            steps.step(
                (
                    "changed",
                    *repository,
                    "--base",
                    rev.base_commit,
                    "--revision",
                    commit,
                ),
                "control",
                None,
                "changed_paths",
            )
        )
    return found


def publish_steps(
    steps: Steps, branch: str, message: str
) -> tuple[tuple[PlanStep, ...], ...]:
    repository = ("--repository", REPOSITORY_ARGUMENT)
    base = ("--base", "{base_commit}")
    revisions = (*base, "--prepared", "{commit}")
    sealed = ("--input", "{input_location}")
    export = (
        steps.step(
            ("export", "--checkout", "{commit}", "--out", EXPORT_DIRECTORY),
            "candidate",
            None,
        ),
    )
    prepare = (
        steps.publisher(
            ("ancestry", *repository, *base), "config_error", cwd="control"
        ),
        steps.publisher(("base", *repository, *base), "infrastructure"),
        steps.publisher(("import", *sealed, *base), "artifact_invalid"),
        steps.publisher(
            ("build", *sealed, *base, "--message", message), "artifact_invalid"
        ),
        steps.publisher(("prepared",), None, "commit"),
    )
    inspect = (
        steps.publisher(("decode", *revisions), "artifact_invalid", "inspection"),
        steps.publisher(("paths", *revisions), None, "changed_paths"),
        steps.publisher(("stat", *revisions), None, "diff_stat"),
    )
    publish = (
        steps.publisher(("publish", *repository, "--branch", branch, *revisions), None),
    )
    return export, prepare, inspect, publish


class DirVcs(LocalVcs, Destinations, InspectionSources, ReadsHeads):
    config_schema: JsonObject = CONFIG_SCHEMA

    def __init__(self, configuration: LocalConfiguration) -> None:
        try:
            resolved = resolve_tools(
                ["prlimit"] if needs_prlimit() else [], configuration.tool_path
            )
        except ToolMissing:
            resolved = {}
        self.helper_tools = HelperTools(
            os.path.realpath(sys.executable), resolved.get("prlimit")
        )
        remotes = {
            repo: located_remote(remote, check_store, ())
            for repo, remote in configuration.remotes.items()
        }
        super().__init__(PROVIDER_NAME, EVENTS, remotes)
        self.steps = Steps(self.helper_tools)
        self.sources = DirInspectionSources()

    @property
    def tools(self) -> Mapping[str, str]:
        return self.helper_tools.mapping()

    def required_tools(self) -> tuple[str, ...]:
        return tuple(self.helper_tools.mapping())

    def repository_file(self, remote: Remote) -> PlanFile:
        document = {
            "path": remote.path,
            "device": remote.pin.device,
            "inode": remote.pin.inode,
        }
        return PlanFile(
            area="control",
            path=REPOSITORY_FILE,
            content=store.canonical(document),
            mode=0o600,
        )

    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan:
        remote = self.remote(request.repo, ctx)
        rev = request.rev
        if isinstance(rev, VcsRef):
            valid = store.is_identifier(rev.commit) and store.is_identifier(
                rev.base_commit
            )
        else:
            valid = store.is_identifier(rev)
        if not valid or not store.is_branch(request.branch):
            self.refuse(ctx, "vcs_request_invalid")
        plan = CheckoutPlan(
            required_tools=("python",),
            credentials={},
            files=(self.repository_file(remote),),
            steps=tuple(checkout_steps(self.steps, rev, request.branch)),
        )
        ctx.logger.emit(self.events.planned, kind="checkout")
        return plan

    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan:
        remote = self.remote(request.repo, ctx)
        if (
            not is_publish_branch(request.branch)
            or store.COMMIT_MESSAGE.fullmatch(request.commit_message) is None
        ):
            self.refuse(ctx, "vcs_request_invalid")
        if needs_prlimit() and self.helper_tools.prlimit is None:
            configuration_refused("tool_missing")
        export, prepare, inspect, publish = publish_steps(
            self.steps, request.branch, request.commit_message
        )
        plan = PublishPlan(
            repo=request.repo,
            branch=request.branch,
            required_tools=self.required_tools(),
            files=(self.repository_file(remote),),
            export=export,
            prepare=prepare,
            inspect=inspect,
            publish=publish,
        )
        ctx.logger.emit(self.events.planned, kind="publish")
        return plan

    def open(
        self, prepared_location: str
    ) -> AbstractAsyncContextManager[InspectionSource]:
        return self.sources.open(prepared_location)

    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        remote = self.remote(ref.repo, ctx)
        if not store.is_branch(ref.branch):
            self.refuse(ctx, "vcs_request_invalid")
        try:
            pin = store.Pin(remote.path, remote.pin.device, remote.pin.inode)
            head = store.read_head(pin, ref.branch)
        except FileNotFoundError:
            head = None
        except (OSError, ValueError, store.Refused, store.Failed):
            self.refuse(ctx, "vcs_remote_unreachable")
        self.require_same(remote, ctx)
        if head is None:
            ctx.logger.emit(self.events.head_read, outcome="missing")
            raise refusal("vcs_ref_missing")
        ctx.logger.emit(self.events.head_read, outcome="found")
        return head
