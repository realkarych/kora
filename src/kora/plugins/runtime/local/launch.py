import asyncio
import hashlib
import hmac
import os
import secrets
import signal
import stat
import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from pydantic import TypeAdapter, ValidationError

from kora.ir.vcs_ref_json import JsonRefusal, decode_strict_json
from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.channel import (
    PRE_ATTACH_BYTES,
    FrameType,
    LaunchCapability,
    LaunchExit,
    LaunchFailure,
    LaunchSignal,
    LaunchSpecEnvelope,
    LaunchSpecRequest,
    LaunchStarted,
)
from kora.plugins.runtime.local.environment import candidate_environment
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.protocols.harness import LaunchEnd, LaunchInputs, Placement
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import EnvironmentVariable
from kora.protocols.secret_value import SecretValue
from kora.protocols.time import Clock
from kora.runner.agent import refused
from kora.runner.channels import (
    BoundedChannel,
    ChannelError,
    ChannelFailure,
    cancel_task,
    wait_future,
)
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    ReleaseState,
    open_private_directory,
    release_tree,
)
from kora.runner.identity import NativeIdentityReader
from kora.runner.native_io import observed
from kora.runner.sink import RunnerSink

COMPONENT: Final[str] = CATALOG.register_component("runtime.local.launcher")
ENDED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.launcher.ended",
        COMPONENT,
        Level.INFO,
        "the harness launch ended",
        {
            "launch_id": FieldKind.STRING,
            "cause": FieldKind.STRING,
            "exit_code": FieldKind.INTEGER,
            "signal": FieldKind.INTEGER,
        },
    )
)
MAX_PENDING_LAUNCHES: Final[int] = 4

REPORT: Final[
    TypeAdapter[LaunchStarted | LaunchExit | LaunchSignal | LaunchFailure]
] = TypeAdapter(LaunchStarted | LaunchExit | LaunchSignal | LaunchFailure)


class BinaryVerifier(Protocol):
    @property
    def path(self) -> Path: ...

    def matches(self) -> bool: ...


@dataclass(frozen=True)
class ExpectedBinary:
    path: Path
    sha256: str
    size: int

    def matches(self) -> bool:
        descriptor = os.open(
            self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
        try:
            entry = os.fstat(descriptor)
            if not stat.S_ISREG(entry.st_mode) or entry.st_size != self.size:
                return False
            digest = hashlib.sha256()
            remaining = self.size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
            return not os.read(descriptor, 1) and digest.hexdigest() == self.sha256
        finally:
            os.close(descriptor)


class LocalLaunch:
    def __init__(
        self,
        owner: "LocalLaunches",
        launch_id: str,
        capability: str,
        spec: LaunchSpecEnvelope,
        verified: bool,
    ) -> None:
        self.owner = owner
        self.launch_id = launch_id
        self.cli_path = owner.path / launch_id / "claude"
        self.capability: str | None = hashlib.sha256(capability.encode()).hexdigest()
        self.spec: LaunchSpecEnvelope | None = spec
        self.identity: ProcessIdentity | None = None
        self.started = False
        self.ready = asyncio.Event()
        self.verified = verified
        self.result: asyncio.Future[LaunchEnd] = (
            asyncio.get_running_loop().create_future()
        )
        self.deadline = owner.clock.monotonic_ns() + 10_000_000_000
        self.timer = asyncio.create_task(self.expire())

    async def expire(self) -> None:
        await self.owner.clock.sleep_until(self.deadline)
        if self.identity is None and not self.result.done():
            try:
                await self.kill()
            except BaseException as error:
                self.finish(LaunchEnd("launch_failed", None, None), error)
                raise
            self.finish(LaunchEnd("launch_failed", None, None))

    def revoke(self) -> None:
        self.capability = None
        self.spec = None
        directory = open_private_directory(self.owner.directory, self.launch_id)
        try:
            try:
                os.unlink("capability", dir_fd=directory.descriptor)
            except FileNotFoundError:
                return
        finally:
            directory.close()

    async def kill(self) -> None:
        if self.result.done():
            return
        if self.identity is None:
            try:
                self.revoke()
                self.signal(self.owner.identities.launcher_children(str(self.cli_path)))
            except BaseException as error:
                self.finish(LaunchEnd("launch_failed", None, None), error)
                raise
            self.finish(LaunchEnd("launch_failed", None, None))
            return
        self.signal((self.identity,))

    def signal(self, identities: tuple[ProcessIdentity, ...]) -> None:
        for identity in identities:
            current = self.owner.identities.read(identity.pid).process
            if (
                current is None
                or current.dead
                or current.identity.start_time != identity.start_time
            ):
                continue
            try:
                if self.started:
                    if current.process_group_id == identity.pid:
                        os.killpg(identity.pid, signal.SIGKILL)
                else:
                    os.kill(identity.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue

    async def ended(self) -> LaunchEnd:
        return await asyncio.shield(self.result)

    def finish(self, outcome: LaunchEnd, failure: BaseException | None = None) -> None:
        if self.result.done():
            return
        if asyncio.current_task() is not self.timer:
            self.timer.cancel()
        try:
            self.release(outcome)
        except BaseException as error:
            failure = failure if failure is not None else error
            raise
        finally:
            self.owner.pending.pop(self.launch_id, None)
            try:
                if self.identity is not None:
                    self.owner.held.discard(self.identity.pid)
                    self.owner.reaper.release(self.identity.pid)
            finally:
                if failure is None:
                    self.result.set_result(outcome)
                else:
                    self.result.set_exception(failure)

    def release(self, outcome: LaunchEnd) -> None:
        self.revoke()
        progress = release_tree(self.owner.directory, self.launch_id)
        if progress.state is not ReleaseState.COMPLETE:
            raise FilesystemError("launch_cleanup_incomplete")
        fields: dict[str, str | int] = {
            "launch_id": self.launch_id,
            "cause": str(outcome.cause),
        }
        if outcome.exit_code is not None:
            fields["exit_code"] = outcome.exit_code
        if outcome.signal is not None:
            fields["signal"] = outcome.signal
        self.owner.log.emit(ENDED, **fields)


class LocalLaunches:
    def __init__(
        self,
        placements: Mapping[Placement, Path],
        control: DirectoryAnchor,
        path: Path,
        socket_path: Path,
        binary: BinaryVerifier | None,
        token: SecretValue,
        clock: Clock,
        identities: NativeIdentityReader,
        log: ComponentLogger,
        reaper: AdoptedReaper,
        harness_credentials: Mapping[str, SecretValue],
    ) -> None:
        self.placements = placements
        self.path = path / "launch"
        self.socket_path = socket_path
        self.binary = binary
        self.harness_credentials: Mapping[str, SecretValue] = MappingProxyType(
            dict(harness_credentials)
        )
        self.token = token
        self.clock = clock
        self.identities = identities
        self.log = log
        self.reaper = reaper
        self.pending: dict[str, LocalLaunch] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.held: set[int] = set()
        self.closed = False
        self.verification: asyncio.Task[bool] | None = None
        self.allocating = 0
        self.closing: asyncio.Task[None] | None = None
        self._sink: RunnerSink | None = None
        os.mkdir("launch", 0o700, dir_fd=control.descriptor)
        try:
            self.directory = open_private_directory(control, "launch")
        except (OSError, FilesystemError):
            release_tree(control, "launch")
            raise

    def bind(self, sink: RunnerSink) -> "LocalLaunches":
        if self._sink is not None or self.closed:
            raise refused("launch_rejected")
        self._sink = sink
        return self

    def _launch_guard(self) -> AbstractContextManager[bool]:
        if self.closed or self._sink is None:
            return nullcontext(not self.closed)
        return self._sink.launch_guard()

    def _launch_allowed(self) -> bool:
        with self._launch_guard() as allowed:
            return allowed

    async def allocate(self, inputs: LaunchInputs) -> LocalLaunch:
        binary = self.binary
        shebang = f"#!{sys.executable} -I\n"
        if (
            not self._launch_allowed()
            or binary is None
            or len(self.pending) + self.allocating >= MAX_PENDING_LAUNCHES
            or not Path(sys.executable).is_absolute()
            or any(character.isspace() for character in sys.executable)
            or len(os.fsencode(shebang)) > 127
            or not binary.path.is_absolute()
            or not inputs.cwd.is_absolute()
        ):
            raise refused("launch_rejected")
        environment = candidate_environment(
            cwd=inputs.cwd,
            attempt_token=self.token,
            workload={},
            secrets={},
            harness_credentials=self.harness_credentials,
            adapter=inputs.environment,
        )
        self.allocating += 1
        try:
            if self.verification is None:
                self.verification = asyncio.create_task(self._verify(binary))
            try:
                if self._sink is not None:
                    await self._sink.wait_for_launch_verification(self.verification)
                    if not self._launch_allowed():
                        raise refused("launch_rejected")
                verified = await asyncio.shield(self.verification)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                raise refused("launch_rejected") from None
        finally:
            self.allocating -= 1
        if not self._launch_allowed():
            raise refused("launch_rejected")
        launch_id = secrets.token_hex(16)
        capability = LaunchCapability(
            launch_id=launch_id,
            capability=secrets.token_hex(32),
            socket_path=str(self.socket_path),
        )
        spec = LaunchSpecEnvelope(
            request_id=launch_id,
            result="launch_spec",
            executable=str(binary.path),
            cwd=str(inputs.cwd),
            environment=tuple(
                EnvironmentVariable(name=name, value=value)
                for name, value in environment.items()
            ),
        )
        os.mkdir(launch_id, 0o700, dir_fd=self.directory.descriptor)
        directory = open_private_directory(self.directory, launch_id)
        try:
            for name, mode, content in (
                ("capability", 0o400, capability.model_dump_json().encode()),
                (
                    "claude",
                    0o500,
                    (
                        f"#!{sys.executable} -I\n"
                        "from kora.plugins.runtime.local.launcher import main\n"
                        "raise SystemExit(main())\n"
                    ).encode(),
                ),
            ):
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    mode,
                    dir_fd=directory.descriptor,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=False) as stream:
                        stream.write(content)
                finally:
                    os.close(descriptor)
        except BaseException:
            release_tree(self.directory, launch_id)
            raise
        finally:
            directory.close()
        launch = LocalLaunch(self, launch_id, capability.capability, spec, verified)
        self.pending[launch_id] = launch
        self.tasks.add(launch.timer)
        launch.timer.add_done_callback(self.tasks.discard)
        return launch

    async def _verify(self, binary: BinaryVerifier) -> bool:
        try:
            return await asyncio.to_thread(binary.matches)
        except OSError:
            return False

    async def _close_verification(self) -> None:
        verification = self.verification
        if verification is None:
            return
        await cancel_task(verification)

    async def serve(
        self, request: LaunchSpecRequest, peer: ProcessIdentity, channel: BoundedChannel
    ) -> None:
        launch = self.pending.get(request.launch_id)
        current = self.identities.read(peer.pid).process
        if (
            launch is None
            or launch.capability is None
            or self.clock.monotonic_ns() >= launch.deadline
            or current is None
            or current.dead
            or current.identity != peer
            or current.parent_pid != os.getpid()
            or current.session_id != os.getsid(0)
            or peer.leads_group
            or peer.pid in self.held
            or not hmac.compare_digest(
                launch.capability,
                hashlib.sha256(request.capability.encode()).hexdigest(),
            )
        ):
            raise refused("launch_rejected")
        try:
            arguments = self.identities.arguments(peer.pid)
        except OSError:
            raise refused("launch_rejected") from None
        if arguments[:1] != (os.fsencode(launch.cli_path),) and arguments[:3] != (
            os.fsencode(sys.executable),
            b"-I",
            os.fsencode(launch.cli_path),
        ):
            raise refused("launch_rejected")
        spec = launch.spec
        launch.identity = peer
        self.reaper.register(peer.pid)
        self.held.add(peer.pid)
        outcome = LaunchEnd("launcher_died", None, None)
        try:
            launch.revoke()
            await cancel_task(launch.timer)
            if not self._launch_allowed():
                return
            if not launch.verified:
                outcome = LaunchEnd("integrity_mismatch", None, None)
                return
            if spec is None:
                raise refused("launch_rejected")
            await channel.write_typed_frame(
                FrameType.JSON,
                spec.model_dump_json().encode(),
                self.clock.monotonic_ns() + 5_000_000_000,
                guard=self._launch_guard,
            )
            spec = None
            while True:
                await channel.wait_readable()
                _, payload = await channel.read_typed_frame(
                    self.clock.monotonic_ns() + 5_000_000_000,
                    {FrameType.JSON.value: PRE_ATTACH_BYTES},
                )
                decoded = decode_strict_json(payload, PRE_ATTACH_BYTES, 1)
                if isinstance(decoded, JsonRefusal):
                    raise ChannelError(ChannelFailure.MALFORMED)
                record = REPORT.validate_python(decoded)
                if isinstance(record, LaunchStarted):
                    if launch.started or record.pgid != peer.pid:
                        break
                    launch.started = True
                    launch.ready.set()
                    continue
                if isinstance(record, LaunchFailure):
                    outcome = LaunchEnd("launch_failed", None, None)
                elif launch.started and isinstance(record, LaunchExit):
                    outcome = LaunchEnd(
                        "cli_exit_without_result", record.exit_code, None
                    )
                elif launch.started and isinstance(record, LaunchSignal):
                    outcome = LaunchEnd("launcher_died", None, record.signal)
                if launch.started and isinstance(record, (LaunchExit, LaunchSignal)):
                    leader = self.identities.read(peer.pid).process
                    if (
                        leader is not None
                        and not leader.dead
                        and leader.identity.start_time == peer.start_time
                        and leader.process_group_id == peer.pid
                    ):
                        try:
                            os.killpg(peer.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            break
                break
        except (ChannelError, ValidationError):
            outcome = LaunchEnd("launcher_died", None, None)
        except OSError:
            outcome = LaunchEnd("launch_failed", None, None)
        finally:
            channel.close()
            launch.finish(outcome)

    async def close(self) -> None:
        if self.closing is None:
            self.closing = asyncio.create_task(self._release())
        cancelled = await observed(self.closing)
        self.closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _release(self) -> None:
        self.closed = True
        pending = tuple(self.pending.values())
        deadline = self.clock.monotonic_ns() + 5_000_000_000
        outcomes: list[object] = []

        async def finish(launch: LocalLaunch) -> None:
            launch.finish(LaunchEnd("launch_failed", None, None))

        try:
            outcomes.extend(
                await asyncio.gather(
                    *(launch.kill() for launch in pending), return_exceptions=True
                )
            )
            outcomes.extend(
                await asyncio.gather(
                    *(
                        wait_future(launch.result, deadline, self.clock)
                        for launch in pending
                    ),
                    return_exceptions=True,
                )
            )
        finally:
            try:
                outcomes.extend(
                    await asyncio.gather(
                        *(finish(launch) for launch in pending), return_exceptions=True
                    )
                )
                outcomes.extend(
                    await asyncio.gather(
                        *(cancel_task(task) for task in tuple(self.tasks)),
                        return_exceptions=True,
                    )
                )
                outcomes.extend(launch.result.exception() for launch in pending)
                outcomes.extend(
                    await asyncio.gather(
                        self._close_verification(), return_exceptions=True
                    )
                )
            finally:
                self.directory.close()
        for outcome in outcomes:
            if (
                isinstance(outcome, ChannelError)
                and outcome.reason is ChannelFailure.DEADLINE
            ):
                raise refused("kill_unconfirmed")
            if isinstance(outcome, BaseException):
                raise outcome
