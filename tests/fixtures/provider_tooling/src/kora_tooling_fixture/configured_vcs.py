import asyncio
import os
import shutil
import socket
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.conformance.vcs import VcsBench, VcsBinding, VcsConformanceInputs, VcsRemote
from kora.executor.authorization import TrustedBinding
from kora.executor.entry import Executor
from kora.executor.launcher import Ending, LaunchSpec, ProcessLauncher, StepOutcome
from kora.ir.json import JsonObject
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.vcs import CredentialRequest, Vcs
from kora.runner.filesystem import DirectoryAnchor
from kora_tooling_fixture.owned_directory import (
    directory_identity,
    dispose_owned_directory,
)


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
        parsed = configured_paths(config)
        root = Path(parsed.remotes["local"].path).parent
        assert root.parent == Path.cwd()
        assert not root.exists()
        root.mkdir(mode=0o700)
        anchor = DirectoryAnchor.open_path(root)
        self.root = root
        self.identity = directory_identity(anchor.descriptor)
        anchor.close()
        git(root, "init", "-q", "--bare", "remote.git")
        git(root, "init", "-q", "--bare", "gone.git")
        git(root, "init", "-q", "seed")
        (root / "seed" / "file.txt").write_text("seed\n")
        git(root, "-C", "seed", "add", ".")
        git(root, "-C", "seed", "commit", "-q", "-m", "seed")
        git(
            root,
            "-C",
            "seed",
            "push",
            "-q",
            str(root / "remote.git"),
            "HEAD:refs/heads/main",
        )
        revision = git(root, "-C", "seed", "rev-parse", "HEAD")
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
