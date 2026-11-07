import asyncio
import os
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import checked_out, prepared_from, published, sealed_input
from kora.executor.launcher import (
    LaunchSpec,
    ProcessLauncher,
    StepLauncher,
    StepOutcome,
)
from kora.executor.scan import InspectionSource
from kora.plugins.vcs.fake import DirVcs
from kora.plugins.vcs.local import LocalConfiguration
from tests.plugins.vcs.dirvcs_support import make_dir_world
from tests.plugins.vcs.git_support import REPO

CYCLES = 2
DEADLINE_SECONDS = 30.0
PROBE = Path(__file__).with_name("dirvcs_publish_probe.py")


@dataclass
class StagedLauncher(StepLauncher):
    ready: Path
    inner: StepLauncher = field(default_factory=ProcessLauncher)

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        if "publish" in spec.argv:
            helper = spec.argv.index("-I") + 1
            argv = (
                *spec.argv[:helper],
                str(PROBE),
                str(self.ready),
                *spec.argv[helper + 1 :],
            )
            argv = tuple(
                sys.executable if item == os.path.realpath(sys.executable) else item
                for item in argv
            )
            spec = replace(spec, argv=argv)
        return await self.inner.run(spec)


class HeldSource(InspectionSource):
    def __init__(self, inner: InspectionSource, gate: asyncio.Event) -> None:
        self.inner = inner
        self.gate = gate

    async def index(self, location: str, limit: int) -> bytes:
        return await self.inner.index(location, limit)

    async def content(self, location: str, limit: int) -> AsyncIterator[bytes]:
        async for block in self.inner.content(location, limit):
            yield block
            self.gate.set()
            await asyncio.Event().wait()

    async def revision_bytes(self, revision: str, limit: int) -> bytes:
        return await self.inner.revision_bytes(revision, limit)

    async def reachable(
        self, revision: str, base: str, timeout_ns: int
    ) -> frozenset[str]:
        return await self.inner.reachable(revision, base, timeout_ns)


class HeldScanVcs(DirVcs):
    def __init__(self, configuration: LocalConfiguration, gate: asyncio.Event) -> None:
        super().__init__(configuration)
        self.gate = gate

    @asynccontextmanager
    async def open(self, prepared_location: str) -> AsyncGenerator[InspectionSource]:
        async with super().open(prepared_location) as source:
            yield HeldSource(source, self.gate)


async def cancel_after(task: asyncio.Task[object], ready: asyncio.Event) -> None:
    observation = asyncio.create_task(ready.wait())
    try:
        done, _ = await asyncio.wait(
            {task, observation},
            timeout=DEADLINE_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert observation in done
    finally:
        observation.cancel()
        await asyncio.gather(observation, return_exceptions=True)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, DEADLINE_SECONDS)


@pytest.mark.parametrize("ending", ["success", "killed_mid_publish", "cancelled_scan"])
def test_dirvcs_publication_releases_resources(
    tmp_path: Path, kora_capture: Capture, ending: str
) -> None:
    baseline = len(os.listdir("/dev/fd"))

    async def exercise() -> None:
        gate = asyncio.Event()
        ready_path = tmp_path / "staged"
        os.mkfifo(ready_path)
        reader = os.open(ready_path, os.O_RDONLY | os.O_NONBLOCK)
        keeper = os.open(ready_path, os.O_WRONLY | os.O_NONBLOCK)
        loop = asyncio.get_running_loop()
        acknowledgements: list[bytes] = []

        def received() -> None:
            try:
                data = os.read(reader, 5)
            except BlockingIOError:
                return
            acknowledgements.append(data)
            gate.set()

        loop.add_reader(reader, received)
        launcher = (
            StagedLauncher(ready_path)
            if ending == "killed_mid_publish"
            else ProcessLauncher()
        )

        def provider(config: LocalConfiguration) -> DirVcs:
            return (
                HeldScanVcs(config, gate)
                if ending == "cancelled_scan"
                else DirVcs(config)
            )

        world = make_dir_world(
            tmp_path, kora_capture, launcher=launcher, provider_type=provider
        )
        live_baseline = len(os.listdir("/dev/fd"))
        try:
            for cycle in range(CYCLES):
                gate.clear()
                branch = world.remote / "heads" / "kora/graph/run-1/agent/1"
                branch.unlink(missing_ok=True)
                checkout = await checked_out(
                    world.provider,
                    world.executor,
                    world.bench().areas(),
                    REPO,
                    world.base,
                    world.context(),
                )
                (world.candidate(checkout) / f"cycle-{cycle}.txt").write_text(
                    f"cycle {cycle}\n"
                )
                plan, sealed = await sealed_input(
                    world.provider,
                    world.executor,
                    world.remote_fixture(),
                    checkout,
                    world.context(),
                )
                preparation = prepared_from(
                    world.executor,
                    world.binding(),
                    checkout,
                    plan,
                    sealed,
                    world.context(),
                )
                if ending == "cancelled_scan":
                    await cancel_after(asyncio.create_task(preparation), gate)
                else:
                    prepared = await preparation
                    publication = published(world.executor, prepared, world.context())
                    if ending == "killed_mid_publish":
                        await cancel_after(asyncio.create_task(publication), gate)
                        assert acknowledgements == [b"ready"] * (cycle + 1)
                        assert world.remote_head() is None
                    else:
                        assert (await publication).state == "pushed"
                assert len(os.listdir("/dev/fd")) == live_baseline
                assert not list(tmp_path.rglob(".tmp-*"))
                assert not list(tmp_path.rglob("incoming-*"))
                assert (
                    len(list(tmp_path.glob("bench-*/case-*/publication/prepared-*")))
                    == cycle + 1
                )
        finally:
            loop.remove_reader(reader)
            os.close(keeper)
            os.close(reader)

    asyncio.run(exercise())

    assert len(os.listdir("/dev/fd")) == baseline
    with pytest.raises(ChildProcessError):
        os.waitpid(-1, os.WNOHANG)
