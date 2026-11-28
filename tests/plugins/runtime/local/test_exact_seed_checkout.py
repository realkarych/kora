import asyncio
import errno
import os
import shutil
import sys
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Final, Literal

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import check, kora_capture
from kora.executor.launcher import LaunchSpec, ProcessLauncher, StepOutcome
from kora.protocols.errors import KoraProtocolError
from kora.protocols.lifetime import ProcessRegistration
from kora.runner.identity import ArgumentMatch, NativeIdentityReader
from kora.testing.local import TestingLocal as LocalComposition
from kora.testing.seed_checkout import SeedCheckoutPhase
from tests.plugins.runtime.local.testing_support import completed_script, composition
from tests.support.event_loop import turn
from tests.support.local_peers import until
from tests.support.local_runtime import protocol_error

__all__ = ["kora_capture"]

pytestmark = pytest.mark.asyncio

TRANSFER_BYTES: Final = 65_536
TRACE_BYTES: Final = 16_384
GIT_TIMEOUT_NS: Final = 10_000_000_000
FILES: Final = (
    ("seed.txt", b"testing composition\n"),
    (
        "script.json",
        b'{"end":"cli_exit_without_result","on_interrupt":"ignore","steps":[]}',
    ),
)
OBJECT_TYPES: Final = (b"commit", b"tree", b"blob", b"blob")


def descriptors() -> set[str]:
    return set(os.listdir("/dev/fd"))


def assert_reaped(registration: ProcessRegistration) -> None:
    with pytest.raises(ChildProcessError):
        os.waitpid(registration.process.pid, os.WNOHANG)


def assert_descriptor_closed(descriptor: int) -> None:
    with pytest.raises(OSError, match=rf"^\[Errno {errno.EBADF}\]") as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


@dataclass
class SourceObservation:
    local: LocalComposition
    candidate: Path
    baseline: set[str]
    phases: list[SeedCheckoutPhase] = field(default_factory=list[SeedCheckoutPhase])
    registrations: list[ProcessRegistration] = field(
        default_factory=list[ProcessRegistration]
    )

    async def observe(
        self, phase: SeedCheckoutPhase, registration: ProcessRegistration
    ) -> None:
        assert not self.candidate.exists()
        source = self.local.seed_checkout.source.anchor
        assert not source.closed
        os.fstat(source.descriptor)
        if phase is SeedCheckoutPhase.SOURCE_SETTLED:
            assert_reaped(registration)
            assert descriptors() == self.baseline
        else:
            reader = NativeIdentityReader()
            argv = (
                (sys.executable, "-I", "-m", "kora.runner.gate")
                if phase is SeedCheckoutPhase.PREPARED
                else await native_source_argv(self.local)
            )
            await until(
                lambda: (
                    reader.carries(registration.process.pid, argv)
                    is ArgumentMatch.EQUAL
                )
            )
            observed = reader.read(registration.process.pid).process
            assert observed is not None
            assert observed.identity == registration.process
        self.phases.append(phase)
        self.registrations.append(registration)

    def assert_released(self) -> None:
        registration, *following = self.registrations
        assert all(other == registration for other in following)
        assert_reaped(registration)
        assert descriptors() == self.baseline


async def git_operation(
    local: LocalComposition,
    directory: Path,
    arguments: tuple[str, ...],
    *,
    stdin: bytes = b"",
    trace: bool = False,
) -> StepOutcome:
    environment = local.seed_checkout.environment()
    if trace:
        environment["GIT_TRACE_REFS"] = "1"
    return await bounded_command(
        (local.seed_checkout.argv[0], *arguments), directory, environment, stdin
    )


async def bounded_command(
    argv: tuple[str, ...], directory: Path, environment: dict[str, str], stdin: bytes
) -> StepOutcome:
    outcome = await ProcessLauncher().run(
        LaunchSpec(
            argv=argv,
            cwd=directory,
            env=environment,
            stdin=stdin,
            timeout_ns=GIT_TIMEOUT_NS,
            stdout_limit=TRANSFER_BYTES + 1,
            stderr_limit=TRACE_BYTES,
        )
    )
    check(outcome.succeeded, "fixture Git failed to return complete output")
    check(not outcome.stdout_overflow, "fixture Git stdout exceeded capture limit")
    check(outcome.stderr_dropped == 0, "fixture Git stderr exceeded capture limit")
    return outcome


async def native_source_argv(local: LocalComposition) -> tuple[str, ...]:
    git, *args = local.seed_checkout.argv
    if sys.platform == "darwin" and git == "/usr/bin/git":
        installed = await bounded_command(
            ("/usr/bin/xcrun", "--find", "git"),
            local.remote,
            local.seed_checkout.environment(),
            b"",
        )
        git = installed.stdout.strip().decode()
        assert Path(git).is_absolute()
    return (git, *args)


async def moved_ref(local: LocalComposition) -> bytes:
    source = ("--git-dir=.",)
    tree_id = local.seed_checkout.object_ids[1]
    identity = ("-c", "user.name=Kora", "-c", "user.email=kora@example.invalid")
    advanced = await git_operation(
        local,
        local.remote,
        (*source, *identity, "commit-tree", tree_id, "-p", local.commit),
        stdin=b"Move the fixture branch\n",
    )
    new_commit = advanced.stdout.strip().decode("ascii")
    assert new_commit != local.commit
    await git_operation(
        local,
        local.remote,
        (*source, "update-ref", "refs/heads/kora", new_commit, local.commit),
    )
    await git_operation(
        local,
        local.remote,
        (
            *source,
            *identity,
            "tag",
            "-a",
            "-m",
            "Trace control",
            "trace-control",
            new_commit,
        ),
    )
    return advanced.stdout


async def observe_source_batch(local: LocalComposition, advanced: bytes) -> None:
    assert "GIT_TRACE_REFS" not in local.seed_checkout.environment()
    object_ids = local.seed_checkout.object_ids
    assert len(set(object_ids)) == 4
    stdin = b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids)
    batch = await git_operation(
        local, local.remote, local.seed_checkout.argv[1:], stdin=stdin, trace=True
    )
    check(not batch.stderr, "exact source batch emitted ref trace or diagnostics")
    assert 0 < len(batch.stdout) <= TRANSFER_BYTES
    frames = BytesIO(batch.stdout)
    for object_id, object_type in zip(object_ids, OBJECT_TYPES, strict=True):
        header = frames.readline()
        oid, kind, size = header.rstrip(b"\n").split(b" ")
        assert header.endswith(b"\n")
        assert (oid, kind) == (object_id.encode("ascii"), object_type)
        assert len(frames.read(int(size))) == int(size)
        assert frames.read(1) == b"\n"
    assert not frames.read(1)

    lookup = await git_operation(
        local,
        local.remote,
        ("--git-dir=.", "rev-parse", "--verify", "refs/heads/kora"),
        trace=True,
    )
    assert lookup.stdout == advanced
    check(
        b"read_raw_ref: refs/heads/kora" in lookup.stderr,
        "positive control did not trace a real source ref lookup",
    )
    enumeration = await git_operation(
        local, local.remote, ("--git-dir=.", "for-each-ref"), trace=True
    )
    check(
        b"iterator_advance: refs/tags/trace-control" in enumeration.stderr,
        "positive control did not trace source ref enumeration",
    )


async def test_exact_seed_checkout_survives_branch_movement(
    tmp_path: Path, kora_capture: Capture
) -> None:
    baseline = descriptors()
    async with composition(tmp_path.resolve(), kora_capture) as local:
        source_descriptor = local.seed_checkout.source.anchor.descriptor
        advanced = await moved_ref(local)
        await observe_source_batch(local, advanced)
        candidate = local.root / "exact-candidate"
        observations = SourceObservation(local, candidate, descriptors())

        async with local.exact_seed_checkout(
            candidate, observe_source=observations.observe
        ) as checkout:
            candidate_descriptor = checkout.anchor.descriptor
            assert checkout.path == candidate
            assert not checkout.anchor.closed
            head = (candidate / ".git/HEAD").read_bytes()
            assert head == local.commit.encode() + b"\n"
            for name, content in FILES:
                assert (candidate / name).read_bytes() == content
            inventory = await git_operation(
                local,
                candidate,
                ("cat-file", "--batch-all-objects", "--batch-check=%(objectname)"),
            )
            assert sorted(inventory.stdout.decode("ascii").splitlines()) == sorted(
                local.seed_checkout.object_ids
            )
            tracked = await git_operation(local, candidate, ("ls-files", "-z"))
            assert tracked.stdout == b"script.json\0seed.txt\0"

        assert observations.phases == [
            SeedCheckoutPhase.PREPARED,
            SeedCheckoutPhase.ACTIVATED,
            SeedCheckoutPhase.SOURCE_SETTLED,
        ]
        assert not candidate.exists()
        assert_descriptor_closed(candidate_descriptor)
        observations.assert_released()

    assert_descriptor_closed(source_descriptor)
    assert descriptors() == baseline
    diagnostics = kora_capture.finish()
    source_markers = ("refs/heads/kora", "trace-control", local.commit, str(tmp_path))
    check(
        not any(
            marker in str(record) for record in diagnostics for marker in source_markers
        ),
        "runtime diagnostics exposed fixture source content",
    )


async def test_exact_seed_checkout_shutdown_settles_after_caller_cancellation(
    tmp_path: Path, kora_capture: Capture
) -> None:
    baseline = descriptors()
    async with composition(tmp_path.resolve(), kora_capture) as local:
        source = local.seed_checkout.source.anchor
        root = local.seed_checkout.root.anchor
        owned_descriptors = (source.descriptor, root.descriptor)

        closing = asyncio.create_task(local.close())
        asyncio.get_running_loop().call_soon(closing.cancel)
        with pytest.raises(asyncio.CancelledError):
            await closing
        async with asyncio.timeout(10):
            while not (source.closed and root.closed):
                await turn()

        for descriptor in owned_descriptors:
            assert_descriptor_closed(descriptor)

    assert descriptors() == baseline


async def test_testing_local_shutdown_refuses_new_workloads_before_runtime_close(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path.resolve(), kora_capture) as local:
        command_id, agent_id = "closing-command", "closing-agent"
        source = completed_script()
        registry = local.root / "state" / "registry"
        cells = tuple(
            registry / f"{local.runtime.registry.key(attempt_id).name}.{suffix}"
            for attempt_id in (command_id, agent_id)
            for suffix in ("json", "lock")
        )

        async def request_workloads() -> tuple[str, str]:
            command = await protocol_error(local.run_command(command_id))
            agent = await protocol_error(local.run_agent(agent_id, source))
            assert not local.runtime.executor.closed
            return command.code, agent.code

        async with asyncio.TaskGroup() as tasks, asyncio.timeout(10):
            tasks.create_task(local.close())
            refused = tasks.create_task(request_workloads())

        assert refused.result() == ("runtime_closed", "runtime_closed")
        assert all(not cell.exists() for cell in cells)


async def test_exact_seed_checkout_shutdown_revokes_prepared_source(
    tmp_path: Path, kora_capture: Capture
) -> None:
    baseline = descriptors()
    async with composition(tmp_path.resolve(), kora_capture) as local:
        source = local.seed_checkout.source.anchor
        root = local.seed_checkout.root.anchor
        owned_descriptors = (source.descriptor, root.descriptor)
        candidate = local.root / "exact-candidate"
        observations = SourceObservation(local, candidate, descriptors())
        prepared = asyncio.Event()
        resumed = asyncio.Event()

        async def pause_prepared(
            phase: SeedCheckoutPhase, registration: ProcessRegistration
        ) -> None:
            await observations.observe(phase, registration)
            if phase is SeedCheckoutPhase.PREPARED:
                prepared.set()
                await resumed.wait()

        async def prepare() -> str:
            with pytest.raises(KoraProtocolError) as refused:
                async with local.exact_seed_checkout(
                    candidate, observe_source=pause_prepared
                ):
                    pytest.fail("checkout reached a candidate after fixture shutdown")
            return refused.value.code

        async with asyncio.TaskGroup() as tasks, asyncio.timeout(10):
            pending = tasks.create_task(prepare())
            try:
                await prepared.wait()
                with pytest.raises(KoraProtocolError) as active:
                    await local.close()
                assert active.value.code == "testing_seed_cleanup_unresolved"
                assert not source.closed
                assert not root.closed
            finally:
                resumed.set()
            checkout_refusal = await pending

        assert checkout_refusal == "testing_seed_binding_invalid"
        assert observations.phases == [SeedCheckoutPhase.PREPARED]
        assert_reaped(observations.registrations[0])
        assert not candidate.exists()
        await local.close()
        for descriptor in owned_descriptors:
            assert_descriptor_closed(descriptor)

    assert descriptors() == baseline


@pytest.mark.parametrize(
    "swap_at", [SeedCheckoutPhase.PREPARED, SeedCheckoutPhase.ACTIVATED]
)
async def test_exact_seed_checkout_refuses_same_commit_in_replaced_repository(
    tmp_path: Path, kora_capture: Capture, swap_at: SeedCheckoutPhase
) -> None:
    baseline = descriptors()
    async with composition(tmp_path.resolve(), kora_capture) as local:
        source_descriptor = local.seed_checkout.source.anchor.descriptor
        original = local.root / "original.git"
        replacement = local.root / "replacement.git"
        shutil.copytree(local.remote, replacement)
        equal_commit = await git_operation(
            local,
            replacement,
            ("--git-dir=.", "rev-parse", "--verify", "refs/heads/kora"),
        )
        assert equal_commit.stdout.strip().decode("ascii") == local.commit
        assert replacement.stat().st_ino != local.remote.stat().st_ino
        candidate = local.root / "exact-candidate"
        observations = SourceObservation(local, candidate, descriptors())

        async def replace_at_boundary(
            phase: SeedCheckoutPhase, registration: ProcessRegistration
        ) -> None:
            await observations.observe(phase, registration)
            if phase is swap_at:
                local.remote.rename(original)
                replacement.rename(local.remote)
                assert os.fstat(source_descriptor).st_ino == original.stat().st_ino

        with pytest.raises(KoraProtocolError) as refused:
            async with local.exact_seed_checkout(
                candidate, observe_source=replace_at_boundary
            ):
                pytest.fail("replacement repository was admitted")

        assert refused.value.code == "testing_seed_binding_invalid"
        assert swap_at in observations.phases
        assert SeedCheckoutPhase.SOURCE_SETTLED not in observations.phases
        assert not candidate.exists()
        observations.assert_released()

    assert_descriptor_closed(source_descriptor)
    assert descriptors() == baseline


@pytest.mark.parametrize("malformed", ["missing", "over_limit"])
async def test_exact_seed_checkout_refuses_invalid_source_frames(
    tmp_path: Path, kora_capture: Capture, malformed: Literal["missing", "over_limit"]
) -> None:
    async with composition(tmp_path.resolve(), kora_capture) as local:
        checkout = local.seed_checkout
        object_ids = checkout.object_ids
        request = (
            b"\n".join(object_id.encode("ascii") for object_id in object_ids) + b"\n"
        )
        original = await git_operation(
            local, local.remote, checkout.argv[1:], stdin=request
        )
        object_id = object_ids[-1]
        loose = local.remote / "objects" / object_id[:2] / object_id[2:]
        loose.unlink()
        if malformed == "over_limit":
            payload = FILES[-1][1]
            grown = (
                TRANSFER_BYTES
                + 1
                - len(original.stdout)
                + len(payload)
                + len(str(len(payload)))
                - len(str(TRANSFER_BYTES))
            )
            assert len(str(grown)) == len(str(TRANSFER_BYTES))
            loose.write_bytes(zlib.compress(f"blob {grown}\0".encode() + b"x" * grown))
        malformed_batch = await git_operation(
            local, local.remote, checkout.argv[1:], stdin=request
        )
        if malformed == "over_limit":
            assert len(malformed_batch.stdout) == TRANSFER_BYTES + 1
        else:
            assert malformed_batch.stdout.endswith(object_id.encode() + b" missing\n")

        candidate = local.root / "exact-candidate"
        observations = SourceObservation(local, candidate, descriptors())

        with pytest.raises(KoraProtocolError) as refused:
            async with local.exact_seed_checkout(
                candidate, observe_source=observations.observe
            ):
                pytest.fail("invalid source object frames were admitted")

        assert refused.value.code == "testing_seed_transfer_invalid"
        assert observations.phases == [
            SeedCheckoutPhase.PREPARED,
            SeedCheckoutPhase.ACTIVATED,
        ]
        assert not candidate.exists()
        observations.assert_released()


@pytest.mark.parametrize(
    "cancel_at", [SeedCheckoutPhase.PREPARED, SeedCheckoutPhase.ACTIVATED]
)
async def test_exact_seed_checkout_cancellation_settles_its_source(
    tmp_path: Path, kora_capture: Capture, cancel_at: SeedCheckoutPhase
) -> None:
    baseline = descriptors()
    async with composition(tmp_path.resolve(), kora_capture) as local:
        source_descriptor = local.seed_checkout.source.anchor.descriptor
        candidate = local.root / "exact-candidate"
        observations = SourceObservation(local, candidate, descriptors())
        reached = asyncio.Event()
        hold = asyncio.Event()

        async def pause_at_boundary(
            phase: SeedCheckoutPhase, registration: ProcessRegistration
        ) -> None:
            await observations.observe(phase, registration)
            if phase is cancel_at:
                reached.set()
                await hold.wait()

        async def prepare() -> None:
            async with local.exact_seed_checkout(
                candidate, observe_source=pause_at_boundary
            ):
                pytest.fail("cancelled preparation reached candidate checkout")

        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(prepare())
            async with asyncio.timeout(10):
                await reached.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending

        assert observations.phases[-1] is cancel_at
        assert not candidate.exists()
        observations.assert_released()

    assert_descriptor_closed(source_descriptor)
    assert descriptors() == baseline
