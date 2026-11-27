import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeVar

from pydantic import ValidationError

from kora.observability import (
    CATALOG,
    ComponentLogger,
    EventSpec,
    FieldKind,
    Level,
    SecretAdmissionError,
    current_diagnostics,
)
from kora.plugins.runtime.local.bound_checkout import BoundCheckout, BoundCheckoutError
from kora.plugins.runtime.local.command import COMMAND_STOP, COMPONENT
from kora.plugins.runtime.local.command_stop import (
    GRACE_NS,
    CommandDomainStop,
    StopOutcome,
)
from kora.plugins.runtime.local.environment import candidate_environment
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.protocols.lifetime import (
    EnvironmentVariable,
    OperationId,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    new_operation_id,
)
from kora.protocols.secret_value import SecretRevealRefused, SecretScope, SecretValue
from kora.protocols.time import Clock
from kora.runner.channels import ChannelError, ChannelFailure, cancel_task
from kora.runner.identity import SCAN_NS, NativeIdentityReader
from kora.runner.installed_executable import (
    ExecutablePin,
    ExecutableVerificationError,
    InstalledExecutable,
    verify_installed_executable,
)
from kora.runner.native_io import NativeExecutor, NativeIOUnresolved, observed
from kora.runner.processes import GatedProcess, ProcessError, start_gated

OUTPUT_BYTES: Final[int] = 8192
Observation = TypeVar("Observation")
SCOPE_VARIABLE: Final[str] = "KORA_SCOPED_SHELL"
BASH_PATH: Final[Path] = Path(
    "/bin/bash" if sys.platform == "darwin" else "/usr/bin/bash"
)
SCOPED_SHELL: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.scoped_shell",
        COMPONENT,
        Level.INFO,
        "the attempt runner handled a scoped shell call",
        {"outcome": FieldKind.STRING},
    )
)


class ScopedShellFailure(StrEnum):
    BUSY = "busy"
    REJECTED = "rejected"
    STOPPED = "stopped"
    EXPIRED = "expired"
    OVERFLOW = "overflow"
    LAUNCH_FAILED = "launch_failed"
    UNRESOLVED = "unresolved"


class ScopedShellError(Exception):
    def __init__(self, reason: ScopedShellFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, repr=False, slots=True, eq=False)
class ScopedShellAdmission:
    command: str
    operation_id: OperationId
    cutoff_ns: int
    current: Callable[[], bool]
    _used: bool = field(default=False, init=False)

    def claim(self) -> None:
        if self._used:
            raise ScopedShellError(ScopedShellFailure.REJECTED)
        object.__setattr__(self, "_used", True)


@dataclass(frozen=True, repr=False, slots=True)
class ScopedShellResult:
    exit_code: int
    stdout: str


def _encoded(value: str | int | dict[str, str | int]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class _Output:
    def __init__(self, scope: SecretScope, overflow: Callable[[], None]) -> None:
        self._overflow = overflow
        self._masker = scope.stream_masker(self._retain)
        self._parts: list[str] = []
        self.raw_bytes = 0
        self._encoded = _encoded({"exit_code": 0, "stdout": ""})
        self._discarded = False

    def feed(self, data: bytes) -> None:
        if not data:
            self._masker.close()
        elif not self._discarded:
            self.raw_bytes += len(data)
            if self.raw_bytes > OUTPUT_BYTES:
                self._overflow()
            else:
                self._masker.feed(data)

    def _retain(self, text: str) -> None:
        if not self._discarded:
            self._encoded += _encoded(text) - 2
            if self._encoded > OUTPUT_BYTES:
                self._overflow()
            else:
                self._parts.append(text)

    def discard(self) -> None:
        self._discarded = True
        self._parts.clear()

    def result(self, exit_code: int) -> ScopedShellResult:
        if self._encoded + _encoded(exit_code) - 1 > OUTPUT_BYTES:
            raise ScopedShellError(ScopedShellFailure.OVERFLOW)
        text = "".join(self._parts)
        self.discard()
        return ScopedShellResult(exit_code, text)


@dataclass
class _Call:
    clock: Clock
    cancelled: Callable[[], bool]
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    reason: ScopedShellFailure | None = None
    started: int | None = None
    output: _Output | None = None
    gate: GatedProcess | None = None
    activation_attempted: bool = False
    stopper: CommandDomainStop | None = None
    native_unresolved: bool = False
    closed: bool = False
    task: asyncio.Task[ScopedShellResult] | None = None
    retry: asyncio.Task[bool] | None = None

    def stop(self, reason: ScopedShellFailure) -> None:
        if self.reason is None:
            self.reason = reason
            self.started = self.clock.monotonic_ns()
            self.stopped.set()
            if self.output is not None:
                self.output.discard()
            if self.gate is not None:
                self.close_gate()

    def close_gate(self) -> None:
        if self.gate is not None:
            try:
                self.gate.close_gate()
            except OSError:
                return


class ScopedShell:
    def __init__(
        self,
        checkout: BoundCheckout,
        executable: InstalledExecutable,
        pin: ExecutablePin,
        *,
        home: Path,
        temporary: Path,
        attempt_token: SecretValue,
        secrets: SecretScope,
        clock: Clock,
        reaper: AdoptedReaper,
        siblings: Callable[[], tuple[ProcessRegistration, ...]],
        log: ComponentLogger | None = None,
    ) -> None:
        if (
            sys.platform not in ("linux", "darwin")
            or not home.is_absolute()
            or not temporary.is_absolute()
            or not attempt_token.belongs_to(secrets)
        ):
            raise ScopedShellError(ScopedShellFailure.REJECTED)
        self._checkout = checkout
        self._executable = executable
        self._pin = pin
        self._home = home
        self._temporary = temporary
        self._attempt_token = attempt_token
        self._secrets = secrets
        self._clock = clock
        self._reaper = reaper
        self._siblings = siblings
        self._call: _Call | None = None
        diagnostics = current_diagnostics()
        self._log = (
            log
            if log is not None
            else (diagnostics.logger(COMPONENT) if diagnostics is not None else None)
        )

    @property
    def occupied(self) -> bool:
        return self._call is not None

    @property
    def stdout_bytes(self) -> int:
        output = None if self._call is None else self._call.output
        return 0 if output is None else output.raw_bytes

    def stop(self) -> None:
        if self._call is not None:
            self._call.stop(ScopedShellFailure.STOPPED)

    async def run(self, admission: ScopedShellAdmission) -> ScopedShellResult:
        if self.occupied:
            self._emit(ScopedShellFailure.BUSY)
            raise ScopedShellError(ScopedShellFailure.BUSY)
        try:
            admission.claim()
        except ScopedShellError as error:
            self._emit(error.reason)
            raise
        caller = asyncio.current_task()
        cancellations = 0 if caller is None else caller.cancelling()
        call = _Call(
            self._clock,
            lambda: caller is not None and caller.cancelling() > cancellations,
        )
        self._call = call
        task = asyncio.create_task(self._run(call, admission))
        call.task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            call.stop(ScopedShellFailure.STOPPED)
            await observed(task)
            raise

    async def settle(self, cutoff_ns: int) -> bool:
        call = self._call
        if call is None:
            return True
        call.stop(ScopedShellFailure.STOPPED)
        if call.task is not None and not await self._settled_by(call.task, cutoff_ns):
            return False
        if self._call is not call:
            return True
        if call.native_unresolved or self._clock.monotonic_ns() >= cutoff_ns:
            return False
        if call.retry is None or call.retry.done():
            if call.retry is not None:
                call.retry.result()
            call.retry = asyncio.create_task(self._retry(call, cutoff_ns))
        retry = call.retry
        return await self._settled_by(retry, cutoff_ns) and retry.result()

    async def _settled_by(
        self, task: asyncio.Future[Observation], cutoff_ns: int
    ) -> bool:
        if self._clock.monotonic_ns() >= cutoff_ns:
            return False
        if task.done():
            return True
        timeout = asyncio.create_task(self._clock.sleep_until(cutoff_ns))
        try:
            await asyncio.wait((task, timeout), return_when=asyncio.FIRST_COMPLETED)
            return task.done() and self._clock.monotonic_ns() < cutoff_ns
        finally:
            await cancel_task(timeout)

    async def _retry(self, call: _Call, cutoff_ns: int) -> bool:
        closed = await self._close(call, cutoff_ns, None)
        if closed:
            self._call = None
        return closed

    def _check(self, call: _Call, admission: ScopedShellAdmission) -> None:
        if call.cancelled():
            call.stop(ScopedShellFailure.STOPPED)
        if call.reason is not None:
            raise ScopedShellError(call.reason)
        if self._clock.monotonic_ns() >= admission.cutoff_ns:
            raise ScopedShellError(ScopedShellFailure.EXPIRED)

    def _current(self, call: _Call, admission: ScopedShellAdmission) -> None:
        self._check(call, admission)
        if not admission.current():
            raise ScopedShellError(ScopedShellFailure.REJECTED)
        self._check(call, admission)

    def _verify(self, cutoff: int) -> None:
        self._checkout.verify(self._checkout.path, cutoff, self._clock)
        if (
            self._executable.path != BASH_PATH
            or verify_installed_executable(BASH_PATH, self._pin, cutoff, self._clock)
            != self._executable
        ):
            raise ScopedShellError(ScopedShellFailure.REJECTED)

    def _launch(self, admission: ScopedShellAdmission, scope: str) -> ProcessLaunch:
        environment = candidate_environment(
            cwd=self._checkout.path,
            attempt_token=self._attempt_token,
            workload={},
            secrets={},
            harness_credentials={},
            adapter={
                "HOME": str(self._home),
                "TMPDIR": str(self._temporary),
                SCOPE_VARIABLE: scope,
            },
        )
        return ProcessLaunch(
            executable=str(BASH_PATH),
            argv=(str(BASH_PATH), "--noprofile", "--norc", "-c", admission.command),
            environment=tuple(
                EnvironmentVariable(name=name, value=value)
                for name, value in environment.items()
            ),
            working_directory_resource_id=admission.operation_id,
            stdin=None,
            stdout=ProcessOutput.CAPTURE,
            stderr=ProcessOutput.CAPTURE,
            domain=ProcessDomain.DETACHED,
        )

    async def _execute(
        self, call: _Call, admission: ScopedShellAdmission, native: NativeExecutor
    ) -> None:
        cutoff = admission.cutoff_ns
        self._current(call, admission)
        reader = NativeIdentityReader(clock=self._clock)
        baseline: dict[int, int] = {}

        def prepare() -> None:
            self._verify(cutoff)
            reader.command_prelaunch(
                baseline, min(cutoff, self._clock.monotonic_ns() + SCAN_NS)
            )

        await native.run(prepare)
        self._check(call, admission)
        scope = new_operation_id()
        launch = self._launch(admission, scope)
        output = _Output(self._secrets, lambda: call.stop(ScopedShellFailure.OVERFLOW))
        call.output = output
        gate = start_gated(
            launch,
            admission.operation_id,
            self._checkout.anchor.descriptor,
            cutoff,
            self._clock,
            reader,
            stdout_sink=output.feed,
            wait_owner=self._reaper,
        )
        call.gate = gate
        registration = await gate.prepare()
        call.stopper = CommandDomainStop(
            registration.process_group_id,
            f"{SCOPE_VARIABLE}={scope}".encode("ascii"),
            self._clock,
            self._reaper.wake,
            baseline,
            leader=registration.process,
            siblings=self._siblings,
        )
        await native.run(lambda: self._verify(cutoff))
        self._current(call, admission)
        call.activation_attempted = True
        await gate.acknowledge()
        exiting = asyncio.create_task(gate.wait_exit(cutoff))
        stopped = asyncio.create_task(call.stopped.wait())
        try:
            ready, _ = await asyncio.wait(
                (exiting, stopped), return_when=asyncio.FIRST_COMPLETED
            )
            if exiting in ready:
                exiting.result()
        finally:
            for task in (exiting, stopped):
                task.cancel()
            await asyncio.gather(exiting, stopped, return_exceptions=True)

    async def _run(
        self, call: _Call, admission: ScopedShellAdmission
    ) -> ScopedShellResult:
        try:
            return await self._complete(call, admission)
        finally:
            if call.closed:
                self._call = None

    async def _complete(
        self, call: _Call, admission: ScopedShellAdmission
    ) -> ScopedShellResult:
        native = NativeExecutor(1, 1)
        try:
            try:
                await self._execute(call, admission, native)
            except ScopedShellError as error:
                call.stop(error.reason)
            except (
                BoundCheckoutError,
                ExecutableVerificationError,
                SecretAdmissionError,
                SecretRevealRefused,
                ValidationError,
            ):
                call.stop(ScopedShellFailure.REJECTED)
            except ChannelError as error:
                call.stop(
                    ScopedShellFailure.EXPIRED
                    if error.reason is ChannelFailure.DEADLINE
                    else ScopedShellFailure.LAUNCH_FAILED
                )
            except (OSError, ProcessError):
                call.stop(ScopedShellFailure.LAUNCH_FAILED)
            except NativeIOUnresolved:
                call.native_unresolved = True
        finally:
            started = min(
                admission.cutoff_ns,
                self._clock.monotonic_ns() if call.started is None else call.started,
            )
            if not await self._close(
                call, started + GRACE_NS + SCAN_NS, started, native
            ):
                if call.output is not None:
                    call.output.discard()
                self._emit(ScopedShellFailure.UNRESOLVED)
                raise ScopedShellError(ScopedShellFailure.UNRESOLVED) from None

        output = call.output
        status = call.gate.returncode if call.gate is not None else None
        try:
            self._current(call, admission)
            if output is None or status is None:
                raise ScopedShellError(ScopedShellFailure.LAUNCH_FAILED)
            result = output.result(status)
        except ScopedShellError as error:
            self._emit(error.reason)
            raise
        self._emit("settled")
        return result

    def _emit(self, outcome: str) -> None:
        if self._log is not None:
            level = Level.INFO if outcome == "settled" else Level.WARNING
            self._log.emit_at(level, SCOPED_SHELL, outcome=outcome)

    async def _close(
        self,
        call: _Call,
        cutoff: int,
        started: int | None,
        native: NativeExecutor | None = None,
    ) -> bool:
        executor = NativeExecutor(1, 1) if native is None else native
        gate, stopper = call.gate, call.stopper
        confirmed = False
        try:
            if gate is None:
                confirmed = True
            elif not call.activation_attempted:
                await gate.release_unactivated(cutoff)
                confirmed = gate.closed and not gate.gate_close_failed
            elif stopper is not None:
                call.close_gate()
                sweep = (
                    asyncio.create_task(executor.run(lambda: stopper.stop(started)))
                    if started is not None
                    else None
                )
                try:
                    if not gate.closed:
                        if gate.returncode is None:
                            await gate.settle(cutoff)
                            await gate.drain(cutoff)
                        gate.release(cutoff)
                finally:
                    if sweep is not None:
                        outcome = await sweep
                        self._log_stop(stopper, outcome, started)
                if not gate.gate_close_failed:
                    confirmed = (
                        await executor.run(lambda: stopper.confirm(cutoff))
                        is StopOutcome.CONFIRMED
                    )
        except (ChannelError, ProcessError, OSError):
            confirmed = False
        except NativeIOUnresolved:
            call.native_unresolved = True
        finally:
            try:
                await executor.close()
            except NativeIOUnresolved:
                call.native_unresolved = True

        call.closed = confirmed and not call.native_unresolved
        return call.closed

    def _log_stop(
        self, stopper: CommandDomainStop, outcome: StopOutcome, started: int | None
    ) -> None:
        if self._log is not None and started is not None:
            self._log.emit_at(
                Level.INFO if outcome is StopOutcome.CONFIRMED else Level.WARNING,
                COMMAND_STOP,
                outcome=outcome,
                waited_ms=max(0, (self._clock.monotonic_ns() - started) // 1_000_000),
                stop_details_available=True,
                native_run_unresolved=False,
                native_close_unresolved=False,
                **dict(stopper.diagnostic_snapshot()),
            )
