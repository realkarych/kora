import asyncio
import os
import shutil
import socket
import stat
import subprocess
import threading
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Final, Literal

from pydantic import StrictBool, StrictInt

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.vcs import VcsBench, VcsBinding, VcsConformanceInputs, VcsRemote
from kora.executor.authorization import TrustedBinding
from kora.executor.entry import Executor
from kora.executor.launcher import Ending, LaunchSpec, ProcessLauncher, StepOutcome
from kora.ir.json import JsonObject
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.boundary import BoundaryModel
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.vcs import CredentialRequest, PathText, Vcs
from kora.runner.filesystem import DirectoryAnchor, FilesystemError
from kora_tooling_fixture.owned_directory import (
    directory_identity,
    dispose_owned_directory,
)

PREPARATION_ENTERED: Final[bytes] = b"prepare_entered\n"
PREPARATION_COMPLETE: Final[bytes] = b"prepare_complete\n"
MAX_PREPARATION_EVIDENCE: Final[int] = 128
PreparationStep = Literal[
    "init_remote",
    "init_gone",
    "init_seed",
    "add_seed",
    "commit_seed",
    "push_seed",
    "read_revision",
]
PreparationFailure = Literal["deadline", "nonzero", "signal", "io"]


class PreparationEvidence(BoundaryModel):
    path: PathText
    device: StrictInt
    inode: StrictInt
    occupy_remote: StrictBool

    def append(self, record: bytes, prefix: int) -> bool:
        path = Path(self.path)
        if not path.is_absolute() or ".." in path.parts:
            return False
        if prefix + len(record) > MAX_PREPARATION_EVIDENCE:
            return False
        try:
            with ExitStack() as resources:
                parent = DirectoryAnchor.open_path(path.parent)
                resources.callback(parent.close)
                descriptor = os.open(
                    path.name,
                    os.O_WRONLY
                    | os.O_APPEND
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | os.O_CLOEXEC,
                    dir_fd=parent.descriptor,
                )
                resources.callback(os.close, descriptor)
                opened = os.fstat(descriptor)
                if (
                    (opened.st_dev, opened.st_ino) != (self.device, self.inode)
                    or not stat.S_ISREG(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_uid != os.getuid()
                    or opened.st_nlink != 1
                    or opened.st_size != prefix
                ):
                    return False
                return os.write(descriptor, record) == len(record)
        except (OSError, FilesystemError):
            return False


def git(root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-10-20T12:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-10-20T12:00:00+00:00",
    }
    result = subprocess.run(
        [executable, *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        check=True,
        timeout=5,
    )
    return result.stdout.decode().strip()


def seed_repositories(root: Path, observation: PreparationEvidence | None) -> str:
    def note_failure(step: PreparationStep, reason: PreparationFailure) -> None:
        if observation is not None:
            observation.append(
                f"git_{step}_{reason}\n".encode("ascii"), len(PREPARATION_ENTERED)
            )

    def prepare_git(step: PreparationStep, *arguments: str) -> str:
        try:
            return git(root, *arguments)
        except subprocess.TimeoutExpired:
            note_failure(step, "deadline")
            raise
        except subprocess.CalledProcessError as error:
            note_failure(step, "signal" if error.returncode < 0 else "nonzero")
            raise
        except OSError:
            note_failure(step, "io")
            raise

    prepare_git("init_remote", "init", "-q", "--bare", "remote.git")
    prepare_git("init_gone", "init", "-q", "--bare", "gone.git")
    prepare_git("init_seed", "init", "-q", "seed")
    (root / "seed" / "file.txt").write_text("seed\n")
    prepare_git("add_seed", "-C", "seed", "add", ".")
    prepare_git("commit_seed", "-C", "seed", "commit", "-q", "-m", "seed")
    prepare_git(
        "push_seed",
        "-C",
        "seed",
        "push",
        "-q",
        str(root / "remote.git"),
        "HEAD:refs/heads/main",
    )
    return prepare_git("read_revision", "-C", "seed", "rev-parse", "HEAD")


class ObservedLauncher(ProcessLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.settled = True

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        self.active += 1
        settled = False
        try:
            result = await super().run(spec)
            settled = result.ending is Ending.EXITED
            return result
        finally:
            self.active -= 1
            self.settled = self.settled and settled


def credential(config: JsonObject) -> CredentialRequest:
    remote = GitConfiguration.model_validate(config).remotes["local"]
    return CredentialRequest(
        binding=remote.binding,
        binding_revision=remote.binding_revision,
        credential_mode=remote.credential_mode,
        repo="local",
        purpose="checkout",
        attempt_id="fixture",
        operation_id=None,
        prepared_digest=None,
        destination_digest=None,
    )


async def failure(
    provider: Vcs, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    request = credential(config).model_copy(update={"repo": "/srv/" + marker})
    return await provider.acquire_credential(request, context)


async def effect(provider: Vcs, config: JsonObject, context: ProtocolContext) -> None:
    issued = await provider.acquire_credential(credential(config), context)
    remote = GitConfiguration.model_validate(config).remotes["local"]
    assert issued.principal.stable_id == remote.principal.stable_id
    assert issued.binding == remote.binding
    assert issued.binding_revision == remote.binding_revision


def configured_paths(config: JsonObject) -> GitConfiguration:
    parsed = GitConfiguration.model_validate(config)
    return parsed.model_copy(
        update={
            "remotes": {
                name: remote.model_copy(
                    update={"path": str(Path(remote.path).absolute())}
                )
                for name, remote in parsed.remotes.items()
            }
        }
    )


class Adapter:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.launcher = ObservedLauncher()
        self.root: Path | None = None
        self.identity: tuple[int, int, int] | None = None
        self.exercises: VcsConformanceInputs
        self.gone_removed = False
        self.benches = 0
        self.native_started: asyncio.Future[None] | None = None
        self.native_release = threading.Event()
        self.native_retained = False

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        supplied = fixture_data.get("vcs_preparation_evidence")
        evidence = (
            PreparationEvidence.model_validate(supplied)
            if supplied is not None
            else None
        )
        observation = (
            evidence
            if evidence is not None and evidence.append(PREPARATION_ENTERED, 0)
            else None
        )
        parsed = configured_paths(config)
        root = Path(parsed.remotes["local"].path).parent
        assert root.parent == Path.cwd()
        assert not root.exists()
        root.mkdir(mode=0o700)
        anchor = DirectoryAnchor.open_path(root)
        self.root = root
        self.identity = directory_identity(anchor.descriptor)
        anchor.close()
        if evidence is not None and evidence.occupy_remote:
            (root / "remote.git").touch(mode=0o600, exist_ok=False)
        revision = seed_repositories(root, observation)
        remote = VcsRemote(
            repo="local",
            revision=revision,
            missing_revision="1" * 40,
            unreachable_repo="gone",
            publish_branch="kora/graph/run-1/agent/1",
            commit_message="kora: agent #1",
        )
        chosen = parsed.remotes["local"]
        from kora.ir import PrincipalRef

        principal = PrincipalRef(
            provider_instance=chosen.principal.provider_instance,
            stable_id=chosen.principal.stable_id,
            kind="local_service",
            display_name=chosen.principal.display_name,
        )
        binding = VcsBinding(
            TrustedBinding(
                binding=chosen.binding,
                binding_revision=chosen.binding_revision,
                credential_mode=chosen.credential_mode,
                principal=principal,
            )
        )
        self.exercises = VcsConformanceInputs(
            failure,
            remote,
            replace(remote, publish_branch="kora/graph/run-head/agent/1"),
            binding,
            self.bench,
            self.effect,
        )
        if observation is not None:
            observation.append(PREPARATION_COMPLETE, len(PREPARATION_ENTERED))
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        return ConstructorArguments((configured_paths(config), self.launcher))

    def native_work(self, loop: asyncio.AbstractEventLoop) -> None:
        started = self.native_started
        root = self.root
        assert started is not None
        assert root is not None
        loop.call_soon_threadsafe(started.set_result, None)
        assert self.native_release.wait(5)
        self.native_retained = root.is_dir()

    def release_native(self) -> None:
        assert self.root is not None
        assert self.root.is_dir()
        self.native_release.set()

    async def effect(
        self, provider: Vcs, config: JsonObject, context: ProtocolContext
    ) -> None:
        await effect(provider, config, context)
        if self.mode == "cancel_native":
            loop = asyncio.get_running_loop()
            self.native_started = loop.create_future()
            work = asyncio.create_task(asyncio.to_thread(self.native_work, loop))
            await self.native_started
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                assert work.cancelled()
            raise ValueError("private-cancelled-native-marker")

    async def bench(
        self, provider: Vcs, config: JsonObject, context: ProtocolContext
    ) -> VcsBench:
        assert isinstance(provider, GitVcs)
        assert self.root is not None
        executor = Executor(provider.tools, self.launcher, provider, provider, provider)
        if not self.gone_removed:
            if self.mode.startswith("decoy_"):
                decoy = GitVcs(configured_paths(config), self.launcher)
                if self.mode == "decoy_destinations":
                    executor.destinations = decoy
                elif self.mode == "decoy_sources":
                    executor.sources = decoy
                else:
                    executor.heads = decoy
            gone = self.root / "gone.git"
            descriptor = os.open(gone, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                dispose_owned_directory(gone, directory_identity(descriptor))
            finally:
                os.close(descriptor)
            self.gone_removed = True
        self.benches += 1
        root = self.root / f"bench-{self.benches}"
        root.mkdir(mode=0o700)
        return VcsBench(executor, root)

    async def cleanup(self, instance: object | None) -> None:
        loop = asyncio.get_running_loop()
        if self.mode == "cancel_native":
            assert not self.native_retained
            loop.call_soon(self.release_native)
        await loop.shutdown_default_executor()
        if self.mode == "cancel_native":
            assert self.native_retained
        assert self.launcher.active == 0
        assert self.launcher.settled
        assert self.root is not None
        assert self.identity is not None
        dispose_owned_directory(self.root, self.identity)
        if self.mode == "cancel_native":
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
                endpoint.connect(os.environ["KORA_FIXTURE_FEEDBACK"])
                endpoint.sendall(b"n")
                assert endpoint.recv(1) == b"a"


def configured() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.VCS, "configured", GitVcs, GitVcs.config_schema, "1"
    )
