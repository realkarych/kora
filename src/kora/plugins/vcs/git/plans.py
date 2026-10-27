import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from kora.executor.seal import EXPORT_DIRECTORY
from kora.executor.steps import AreaName
from kora.ir import VcsRef
from kora.plugins.vcs.git.environment import (
    Option,
    export_options,
    git_environment,
    publisher_options,
    push_options,
    trusted_options,
)
from kora.plugins.vcs.git.tools import GitTools
from kora.plugins.vcs.local import limited_argv
from kora.protocols.vcs import (
    Capture,
    CheckoutPlan,
    PlanStep,
    PublishPlan,
    StepFailureClass,
)

HELPERS: Final[Path] = Path(__file__).parent
EXPORT_HELPER: Final[str] = str(HELPERS / "export.py")
TREE_HELPER: Final[str] = str(HELPERS / "tree_build.py")
DECODER_HELPER: Final[str] = str(HELPERS / "decoder.py")
OBJECT_ID: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REF_FORBIDDEN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
COMMIT_MESSAGE: Final[re.Pattern[str]] = re.compile(r"kora: \S+ #[1-9][0-9]*")
PUBLISH_PREFIX: Final[str] = "kora/"
REPOSITORY: Final[str] = "repo"
INDEX_FILE: Final[str] = "index.jsonl"
PREPARED_REF: Final[str] = "refs/kora/prepared"
BASE_REF: Final[str] = "refs/kora/base"
CHECKOUT_REF: Final[str] = "refs/kora/checkout"
CHECKOUT_BASE_REF: Final[str] = "refs/kora/checkout-base"
SHORT: Final[timedelta] = timedelta(minutes=1)
TRANSFER: Final[timedelta] = timedelta(minutes=30)
WORK: Final[timedelta] = timedelta(minutes=30)
CONTROL_HOME: Final[str] = "{control_root}/home"
CANDIDATE_HOME: Final[str] = "{candidate_root}/.kora/home"


def is_object_id(value: str) -> bool:
    return OBJECT_ID.fullmatch(value) is not None


def is_ref_name(name: str) -> bool:
    if (
        not name
        or name.startswith(("-", "/"))
        or name.endswith(("/", "."))
        or ".." in name
        or "//" in name
        or "@{" in name
        or name == "@"
        or REF_FORBIDDEN.search(name) is not None
        or "/" not in name
    ):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in name.split("/")
    )


def is_branch(name: str) -> bool:
    return not name.startswith("-") and is_ref_name(f"refs/heads/{name}")


def is_full_ref(name: str) -> bool:
    return name.startswith("refs/") and is_ref_name(name)


def is_publish_branch(name: str) -> bool:
    return name.startswith(PUBLISH_PREFIX) and is_branch(name)


def is_commit_message(text: str) -> bool:
    return COMMIT_MESSAGE.fullmatch(text) is not None


@dataclass(frozen=True)
class CommitIdentity:
    name: str
    email: str


@dataclass(frozen=True)
class PlanSettings:
    tools: GitTools
    remote: str
    identity: CommitIdentity


def git_date(moment: datetime) -> str:
    return f"@{int(moment.timestamp())} +0000"


class Steps:
    def __init__(
        self,
        settings: PlanSettings,
        cwd: AreaName,
        options: Sequence[Option],
        home: str = CONTROL_HOME,
    ) -> None:
        self.settings = settings
        self.cwd: AreaName = cwd
        self.environment = git_environment(settings.tools.git, home, options)

    def limited(
        self, tool: str, path: str, arguments: Sequence[str]
    ) -> tuple[str, ...]:
        return limited_argv(self.settings.tools.prlimit, tool, path, arguments)

    def step(
        self,
        argv: tuple[str, ...],
        timeout: timedelta,
        failure: StepFailureClass,
        capture: Capture | None = None,
        cwd: AreaName | None = None,
    ) -> PlanStep:
        return PlanStep(
            argv=argv,
            cwd=cwd or self.cwd,
            env=self.environment,
            stdin=None,
            timeout=timeout,
            capture=capture,
            failure_class=failure,
        )

    def git(
        self,
        arguments: Sequence[str],
        timeout: timedelta,
        failure: StepFailureClass,
        capture: Capture | None = None,
        cwd: AreaName | None = None,
    ) -> PlanStep:
        return self.step(("git", *arguments), timeout, failure, capture, cwd)

    def limited_git(
        self,
        arguments: Sequence[str],
        timeout: timedelta,
        failure: StepFailureClass,
        capture: Capture | None = None,
    ) -> PlanStep:
        argv = self.limited("git", self.settings.tools.git, arguments)
        return self.step(argv, timeout, failure, capture)

    def helper(
        self,
        script: str,
        arguments: Sequence[str],
        timeout: timedelta,
        failure: StepFailureClass,
        capture: Capture | None = None,
        limited: bool = True,
    ) -> PlanStep:
        python = self.settings.tools.python
        full = ("-I", script, "--git", self.settings.tools.git, *arguments)
        argv = self.limited("python", python, full) if limited else ("python", *full)
        return self.step(argv, timeout, failure, capture)


def checkout_plan(
    settings: PlanSettings, rev: str | VcsRef, branch: str
) -> CheckoutPlan:
    options = trusted_options("{control_root}/hooks", ("{candidate_root}",))
    steps = Steps(settings, "candidate", options)
    remote = settings.remote
    commit = rev.commit if isinstance(rev, VcsRef) else rev
    requested = (commit, rev.base_commit) if isinstance(rev, VcsRef) else (commit,)
    plan_steps = [
        steps.git(("init", "--quiet", "--template="), SHORT, "infrastructure"),
        steps.git(
            ("ls-remote", "--end-of-options", remote),
            SHORT,
            "infrastructure",
            cwd="control",
        ),
        *(
            steps.git(
                (
                    f"--git-dir={remote}",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "--end-of-options",
                    f"{revision}^0",
                ),
                SHORT,
                "config_error",
                cwd="control",
            )
            for revision in requested
        ),
        steps.git(
            (
                "fetch",
                "--quiet",
                "--no-tags",
                "--end-of-options",
                remote,
                f"+{commit}:{CHECKOUT_REF}",
                *(
                    (f"+{rev.base_commit}:{CHECKOUT_BASE_REF}",)
                    if isinstance(rev, VcsRef)
                    else ()
                ),
            ),
            TRANSFER,
            "infrastructure",
        ),
        steps.git(
            ("checkout", "--quiet", "-B", branch, CHECKOUT_REF, "--"),
            WORK,
            "infrastructure",
        ),
        steps.git(
            ("rev-parse", "--verify", "--end-of-options", "HEAD^0"),
            SHORT,
            "infrastructure",
            "commit",
        ),
        steps.git(
            ("symbolic-ref", "--short", "HEAD"), SHORT, "infrastructure", "branch"
        ),
    ]
    if isinstance(rev, VcsRef):
        plan_steps.append(
            steps.helper(
                DECODER_HELPER,
                (
                    "changed",
                    "--repo",
                    ".git",
                    "--base",
                    rev.base_commit,
                    "--prepared",
                    rev.commit,
                ),
                WORK,
                "infrastructure",
                "changed_paths",
                limited=False,
            )
        )
    return CheckoutPlan(
        required_tools=settings.tools.names(),
        credentials={},
        files=(),
        steps=tuple(plan_steps),
    )


def publish_plan(
    settings: PlanSettings, branch: str, message: str, moment: datetime
) -> PublishPlan:
    exporting = Steps(settings, "candidate", export_options(), CANDIDATE_HOME)
    hooks = "{control_root}/hooks"
    publisher = Steps(
        settings,
        "publication",
        publisher_options(hooks, ("{publication_root}/repo",)),
    )
    pusher = Steps(settings, "publication", push_options())
    git_dir = f"--git-dir={REPOSITORY}"
    remote = settings.remote
    identity = settings.identity
    export = (
        exporting.helper(
            EXPORT_HELPER,
            ("--checkout", "{commit}", "--out", EXPORT_DIRECTORY),
            WORK,
            "artifact_invalid",
            limited=False,
        ),
    )
    prepare = (
        publisher.git(
            ("init", "--bare", "--quiet", "--template=", REPOSITORY),
            SHORT,
            "infrastructure",
        ),
        publisher.git(
            (
                git_dir,
                "fetch",
                "--quiet",
                "--no-tags",
                "--end-of-options",
                remote,
                f"+{{commit}}:{BASE_REF}",
            ),
            TRANSFER,
            "infrastructure",
        ),
        publisher.helper(
            TREE_HELPER,
            (
                "import",
                "--repo",
                REPOSITORY,
                "--input",
                "{input_location}",
                "--base",
                "{commit}",
            ),
            WORK,
            "artifact_invalid",
        ),
        publisher.helper(
            TREE_HELPER,
            (
                "build",
                "--repo",
                REPOSITORY,
                "--input",
                "{input_location}",
                "--base",
                "{commit}",
                "--message",
                message,
                "--name",
                identity.name,
                "--email",
                identity.email,
                "--date",
                git_date(moment),
            ),
            WORK,
            "artifact_invalid",
        ),
        publisher.limited_git(
            (git_dir, "fsck", "--strict", "--no-progress", "--no-dangling"),
            WORK,
            "artifact_invalid",
        ),
        publisher.git(
            (git_dir, "rev-parse", "--verify", "--end-of-options", f"{PREPARED_REF}^0"),
            SHORT,
            "infrastructure",
            "commit",
        ),
    )
    inspect = (
        publisher.helper(
            DECODER_HELPER,
            (
                "index",
                "--repo",
                REPOSITORY,
                "--base",
                "{base_commit}",
                "--prepared",
                "{commit}",
                "--out",
                INDEX_FILE,
            ),
            WORK,
            "artifact_invalid",
            "inspection",
        ),
        publisher.helper(
            DECODER_HELPER,
            (
                "paths",
                "--repo",
                REPOSITORY,
                "--base",
                "{base_commit}",
                "--prepared",
                "{commit}",
            ),
            WORK,
            "artifact_invalid",
            "changed_paths",
        ),
        publisher.limited_git(
            (
                git_dir,
                "diff",
                "--shortstat",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "{base_commit}",
                "{commit}",
                "--",
            ),
            WORK,
            "artifact_invalid",
            "diff_stat",
        ),
    )
    publish = (
        pusher.git(
            (
                git_dir,
                "push",
                "--porcelain",
                "--atomic",
                "--no-verify",
                f"--force-with-lease=refs/heads/{branch}:",
                "--end-of-options",
                remote,
                f"{{commit}}:refs/heads/{branch}",
            ),
            TRANSFER,
            "infrastructure",
        ),
    )
    return PublishPlan(
        required_tools=settings.tools.names(),
        files=(),
        export=export,
        prepare=prepare,
        inspect=inspect,
        publish=publish,
    )
