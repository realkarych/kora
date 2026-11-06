import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from kora.executor.entry import Destinations, ReadsHeads
from kora.executor.steps import AreaName
from kora.ir import VcsRef
from kora.ir.json import JsonObject, parse_json_object
from kora.plugins.vcs.fake import store
from kora.plugins.vcs.local import (
    LocalConfiguration,
    LocalEvents,
    LocalVcs,
    Remote,
    RemoteRefused,
    local_events,
    located_remote,
    refusal,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.vcs import (
    Capture,
    CheckoutPlan,
    CheckoutRequest,
    PlanFile,
    PlanStep,
    StepFailureClass,
)

PROVIDER_NAME: Final[str] = "fake"
HELPER: Final[str] = store.__file__
REPOSITORY_FILE: Final[str] = "repository.json"
REPOSITORY_ARGUMENT: Final[str] = "{control_root}/" + REPOSITORY_FILE
SHORT: Final[timedelta] = timedelta(minutes=1)
WORK: Final[timedelta] = timedelta(minutes=30)
EVENTS: Final[LocalEvents] = local_events("vcs.fake", PROVIDER_NAME)
CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    LocalConfiguration.model_json_schema()
)


@dataclass(frozen=True)
class HelperTools:
    python: str

    def mapping(self) -> dict[str, str]:
        return {"python": self.python}


def check_store(path: str) -> None:
    for section in (*store.SECTIONS, "heads"):
        try:
            status = os.stat(Path(path) / section, follow_symlinks=False)
        except OSError:
            raise RemoteRefused("remote_not_a_store") from None
        if not stat.S_ISDIR(status.st_mode):
            raise RemoteRefused("remote_not_a_store")


class Steps:
    def __init__(self, tools: HelperTools) -> None:
        self.tools = tools

    def step(
        self,
        arguments: Sequence[str],
        cwd: AreaName,
        failure: StepFailureClass | None,
        capture: Capture | None = None,
        timeout: timedelta = WORK,
    ) -> PlanStep:
        full = ("-I", HELPER, *arguments)
        return PlanStep(
            argv=("python", *full),
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


class DirVcs(LocalVcs, Destinations, ReadsHeads):
    config_schema: JsonObject = CONFIG_SCHEMA

    def __init__(self, configuration: LocalConfiguration) -> None:
        self.helper_tools = HelperTools(os.path.realpath(sys.executable))
        remotes = {
            repo: located_remote(remote, check_store, ())
            for repo, remote in configuration.remotes.items()
        }
        super().__init__(PROVIDER_NAME, EVENTS, remotes)
        self.steps = Steps(self.helper_tools)

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
            required_tools=self.required_tools(),
            credentials={},
            files=(self.repository_file(remote),),
            steps=tuple(checkout_steps(self.steps, rev, request.branch)),
        )
        ctx.logger.emit(self.events.planned, kind="checkout")
        return plan

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
