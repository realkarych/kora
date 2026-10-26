import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from kora.conformance.capture import Capture
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.conformance.vcs import (
    Checkout,
    Kept,
    Prepared,
    VcsBench,
    VcsBinding,
    VcsRemote,
    checked_out,
    pinned,
    prepared_from,
    publish_request,
    published,
    run,
    sealed_input,
)
from kora.executor.authorization import TrustedBinding
from kora.executor.entry import Executor, ExecutorSettings, PublishOutcome
from kora.executor.launcher import ProcessLauncher, StepLauncher
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ClockSample, ExecutionDeadline
from kora.protocols.secret_value import SecretValue
from kora.protocols.time import Clock

REPO: Final[str] = "local"
GONE_REPO: Final[str] = "gone"
BINDING: Final[str] = "local-binding"
GONE_BINDING: Final[str] = "gone-binding"
MISSING_REVISION: Final[str] = "1" * 40
PUBLISH_BRANCH: Final[str] = "kora/graph/run-1/agent/1"
COMMIT_MESSAGE: Final[str] = "kora: agent #1"
PROVIDER_INSTANCE: Final[str] = "kora-local"
STABLE_ID: Final[str] = "kora-publisher-7"
AUTHOR_DATE: Final[str] = "2026-10-20T12:00:00+00:00"
GIT: Final[str] = shutil.which("git") or "/usr/bin/git"


def seeded_environment(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Seed Author",
        "GIT_AUTHOR_EMAIL": "seed@example.invalid",
        "GIT_COMMITTER_NAME": "Seed Author",
        "GIT_COMMITTER_EMAIL": "seed@example.invalid",
        "GIT_AUTHOR_DATE": AUTHOR_DATE,
        "GIT_COMMITTER_DATE": AUTHOR_DATE,
    }


def git(
    home: Path, *arguments: str, cwd: Path | None = None, extra: Mapping[str, str] = {}
) -> str:
    completed = subprocess.run(
        [GIT, *arguments],
        cwd=cwd,
        env={**seeded_environment(home), **extra},
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


class ManualClock(Clock):
    def __init__(self) -> None:
        self.wall = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        self.offset_ns = 0
        self.origin_ns = 10**12

    def now(self) -> datetime:
        return self.wall + timedelta(microseconds=self.offset_ns // 1000)

    def monotonic_ns(self) -> int:
        return self.origin_ns + self.offset_ns

    def sample(self) -> ClockSample:
        return ClockSample(wall=self.now(), monotonic_ns=self.monotonic_ns())

    async def sleep_until(self, deadline_ns: int) -> None:
        self.offset_ns = max(self.offset_ns, deadline_ns - self.origin_ns)

    def advance(self, seconds: float) -> None:
        self.offset_ns += int(seconds * 10**9)


@dataclass
class World:
    root: Path
    home: Path
    remote: Path
    seed: Path
    base: str
    provider: GitVcs
    executor: Executor
    capture: Capture
    clock: Clock
    cases: list[int] = field(default_factory=lambda: [0])

    def context(self, deadline: ExecutionDeadline | None = None) -> ProtocolContext:
        return ProtocolContext(
            namespace_id="namespace",
            run_id="run-1",
            attempt_id="conformance-attempt",
            logger=self.capture.diagnostics.logger(CONFORMANCE_COMPONENT),
            secrets=None,
            secret_admission=self.capture.scope.admission(),
            request_digest="sealed-request",
            deadline=deadline,
            clock=self.clock,
            activity=NoActivity(),
        )

    def bench(self) -> VcsBench:
        self.cases[0] += 1
        bench_root = self.root / f"bench-{self.cases[0]}"
        bench_root.mkdir()
        return VcsBench(self.executor, bench_root)

    def remote_fixture(self) -> VcsRemote:
        return VcsRemote(
            repo=REPO,
            revision=self.base,
            missing_revision=MISSING_REVISION,
            unreachable_repo=GONE_REPO,
            publish_branch=PUBLISH_BRANCH,
            commit_message=COMMIT_MESSAGE,
        )

    def binding(self) -> VcsBinding:
        return VcsBinding(
            TrustedBinding(
                binding=BINDING,
                binding_revision=1,
                credential_mode="local_credential_free",
                principal=self.provider.remotes[REPO].principal,
            )
        )

    def checkout(
        self, revision: str | None = None, ctx: ProtocolContext | None = None
    ) -> Checkout:
        return run(
            checked_out(
                self.provider,
                self.executor,
                self.bench().areas(),
                REPO,
                revision or self.base,
                ctx or self.context(),
            )
        )

    def candidate(self, checkout: Checkout) -> Path:
        candidate = checkout.areas.candidate
        assert candidate is not None
        return candidate

    def commit(
        self,
        checkout: Checkout,
        files: Mapping[str, str | None],
        message: str,
        author: str = "Agent Author",
    ) -> str:
        candidate = self.candidate(checkout)
        write(candidate, files)
        git(self.home, "add", "-A", cwd=candidate)
        git(
            self.home,
            "commit",
            "-q",
            "-m",
            message,
            cwd=candidate,
            extra={
                "GIT_AUTHOR_NAME": author,
                "GIT_AUTHOR_EMAIL": "agent@example.invalid",
            },
        )
        return git(self.home, "rev-parse", "HEAD", cwd=candidate)

    def prepare(
        self,
        checkout: Checkout,
        secrets: tuple[SecretValue, ...] = (),
        protected: tuple[str, ...] = (),
        ctx: ProtocolContext | None = None,
    ) -> Prepared:
        context = ctx or self.context()
        remote = self.remote_fixture()
        plan, sealed = run(
            sealed_input(self.provider, self.executor, remote, checkout, context)
        )
        if not secrets and not protected:
            return run(
                prepared_from(
                    self.executor,
                    remote,
                    self.binding(),
                    checkout,
                    plan,
                    sealed,
                    context,
                )
            )
        result = run(
            self.executor.prepare_and_inspect(
                plan,
                sealed,
                checkout.workspace(),
                publish_request(remote, checkout),
                pinned(self.binding()),
                checkout.areas,
                secrets,
                protected,
                Kept(),
                context,
            )
        )
        return Prepared(checkout, plan, sealed, result)

    def publish(self, prepared: Prepared) -> PublishOutcome:
        return run(published(self.executor, prepared, self.context()))

    def remote_head(self, branch: str = PUBLISH_BRANCH) -> str | None:
        completed = subprocess.run(
            [
                GIT,
                f"--git-dir={self.remote}",
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            env=seeded_environment(self.home),
            capture_output=True,
            check=False,
        )
        return completed.stdout.decode().strip() or None


def write(root: Path, files: Mapping[str, str | None]) -> None:
    for name, content in files.items():
        path = root / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def configuration(remote: Path, gone: Path) -> dict[str, object]:
    principal: dict[str, object] = {
        "provider_instance": PROVIDER_INSTANCE,
        "stable_id": STABLE_ID,
        "display_name": "kora local publisher",
    }
    return {
        "remotes": {
            REPO: {
                "path": str(remote),
                "binding": BINDING,
                "binding_revision": 1,
                "credential_mode": "local_credential_free",
                "principal": principal,
            },
            GONE_REPO: {
                "path": str(gone),
                "binding": GONE_BINDING,
                "binding_revision": 1,
                "credential_mode": "local_credential_free",
                "principal": principal,
            },
        },
        "identity": {"name": "kora", "email": "kora@example.invalid"},
        "tool_path": os.environ["PATH"].split(":"),
    }


def seeded_remote(root: Path, home: Path) -> tuple[Path, Path, str]:
    remote = root / "remote.git"
    seed = root / "seed"
    git(home, "init", "-q", "--bare", str(remote))
    git(home, "init", "-q", str(seed))
    write(seed, {"a.txt": "alpha\n", "keep/old.txt": "old\n", "run.sh": "#!/bin/sh\n"})
    (seed / "run.sh").chmod(0o755)
    git(home, "add", "-A", cwd=seed)
    git(home, "commit", "-q", "-m", "seed", cwd=seed)
    git(home, "push", "-q", str(remote), "HEAD:refs/heads/main", cwd=seed)
    return remote, seed, git(home, "rev-parse", "HEAD", cwd=seed)


def make_world(
    root: Path,
    capture: Capture,
    launcher: StepLauncher | None = None,
    settings: ExecutorSettings | None = None,
    clock: Clock | None = None,
    provider_type: type[GitVcs] = GitVcs,
) -> World:
    root = root.resolve()
    home = root / "home"
    home.mkdir()
    remote, seed, base = seeded_remote(root, home)
    gone = root / "gone.git"
    git(home, "init", "-q", "--bare", str(gone))
    provider = provider_type(
        GitConfiguration.model_validate(configuration(remote, gone)),
        launcher or ProcessLauncher(),
    )
    shutil.rmtree(gone)
    executor = Executor(
        provider.tools,
        launcher or ProcessLauncher(),
        provider,
        provider,
        provider,
        settings,
    )
    return World(
        root,
        home,
        remote,
        seed,
        base,
        provider,
        executor,
        capture,
        clock or ManualClock(),
    )
