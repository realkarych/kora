import asyncio
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import MappingProxyType

from pydantic import ValidationError

from kora.plugins.runtime.local.bound_checkout import BoundCheckout, BoundCheckoutError
from kora.plugins.runtime.local.bound_gate_fence import (
    BoundGateFailure,
    BoundGateFence,
    GateAcquisitionFailure,
    GatePhaseFailure,
)
from kora.plugins.runtime.local.bound_role_configuration import (
    BoundRoleConfiguration,
    BoundRoleConfigurationError,
)
from kora.plugins.runtime.local.bound_slots import BoundRoleSlots, BoundSlotDeadline
from kora.plugins.runtime.local.environment import (
    BoundCandidateEnvironments,
    CandidateRoleEnvironment,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ProcessRegistration,
    ResourceId,
    new_operation_id,
)
from kora.protocols.secret_value import SecretRevealRefused
from kora.protocols.time import Clock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.installed_executable import InstalledExecutable
from kora.runner.process_stdio import StdioStreams
from kora.runner.processes import GatedProcess, ProcessError, start_gated


@dataclass(frozen=True, repr=False)
class BoundNativeRole:
    argv: tuple[str, ...]
    cwd: Path
    environment: CandidateRoleEnvironment
    configuration: BoundRoleConfiguration


@dataclass(frozen=True, repr=False)
class _RoleGateInputs:
    argv: tuple[str, ...]
    configuration: BoundRoleConfiguration


class _GatePhase(Enum):
    ACQUIRED = auto()
    PREPARING = auto()
    READY = auto()
    ACTIVATING = auto()
    UNDELIVERED = auto()
    DELIVERING = auto()
    DELIVERED = auto()
    FAILED = auto()


class BoundNativeGate:
    def __init__(
        self,
        gate: GatedProcess,
        fence: BoundGateFence,
        clock: Clock,
        cutoff_ns: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._gate = gate
        self._fence = fence
        self._clock = clock
        self._cutoff_ns = cutoff_ns
        self._loop = loop
        self._phase = _GatePhase.ACQUIRED

    @property
    def closed(self) -> bool:
        return self._gate.closed

    def _require_open(self) -> None:
        if self._fence.stopped:
            raise ChannelError(ChannelFailure.CLOSED)
        if self._clock.monotonic_ns() >= self._cutoff_ns:
            raise ChannelError(ChannelFailure.DEADLINE)

    @contextmanager
    def _transition(
        self, expected: _GatePhase, pending: _GatePhase, completed: _GatePhase
    ) -> Generator[None]:
        if (
            _current_loop() is not self._loop
            or self._fence.stopped
            or self._gate not in self._fence.gates
        ):
            raise ChannelError(ChannelFailure.CLOSED)
        if self._phase is not expected:
            raise ChannelError(ChannelFailure.BUSY)
        self._phase = pending
        cause: BoundGateFailure = GatePhaseFailure.UNEXPECTED
        try:
            self._require_open()
            yield
            self._require_open()
            self._phase = completed
        except asyncio.CancelledError:
            cause = GatePhaseFailure.CANCELLED
            raise
        except ChannelError as error:
            cause = error.reason
            raise
        except ProcessError as error:
            cause = error.condition
            raise
        finally:
            if self._phase is not completed:
                self._phase = _GatePhase.FAILED
                self._fence.fail(cause)

    async def prepare(self) -> ProcessRegistration:
        with self._transition(
            _GatePhase.ACQUIRED, _GatePhase.PREPARING, _GatePhase.READY
        ):
            return await self._gate.prepare()

    async def acknowledge(self) -> None:
        with self._transition(
            _GatePhase.READY, _GatePhase.ACTIVATING, _GatePhase.UNDELIVERED
        ):
            await self._gate.acknowledge()

    def borrow_stdio(self) -> StdioStreams:
        with self._transition(
            _GatePhase.UNDELIVERED, _GatePhase.DELIVERING, _GatePhase.DELIVERED
        ):
            return self._gate.borrow_stdio()


class BoundNativeLaunch:
    def __init__(
        self,
        binding: BoundCandidateEnvironments,
        roles: Mapping[str, BoundNativeRole],
        *,
        executable: InstalledExecutable,
        checkout: BoundCheckout,
        directory_id: ResourceId,
        domain: ProcessDomain,
        stdio_limit: int,
        clock: Clock,
        cutoff_ns: int,
    ) -> None:
        loop = _current_loop()
        captured = dict(roles)
        if (
            loop is None
            or not captured
            or captured.keys() != binding.roles
            or type(stdio_limit) is not int
            or stdio_limit < 1
        ):
            raise _binding_refused()
        self._checkout = checkout
        self._executable = executable
        self._directory_id = directory_id
        self._domain = domain
        self._stdio_limit = stdio_limit
        self._clock = clock
        self._cutoff_ns = cutoff_ns
        self._slots = BoundRoleSlots(binding, clock, cutoff_ns)
        self._fence = BoundGateFence(self._slots)
        self._loop = loop
        self._root_turn_admitted = False
        inputs: dict[str, _RoleGateInputs] = {}
        placements: set[tuple[int, int]] = set()
        for role, recipe in captured.items():
            self._slots.require_open()
            configuration = recipe.configuration
            identities = frozenset(configuration.placement_identities)
            argv = tuple(recipe.argv)
            directories = configuration.directories
            adapter = recipe.environment.adapter
            if (
                recipe.cwd != checkout.path
                or not argv
                or argv[0] != str(executable.path)
                or not binding.matches(role, recipe.cwd, recipe.environment)
                or adapter.get("HOME") != str(directories.home)
                or adapter.get("CODEX_HOME") != str(directories.codex_home)
                or adapter.get("TMPDIR") != str(directories.temporary)
                or len(identities) != 3
                or not placements.isdisjoint(identities)
            ):
                raise _binding_refused()
            self._process_launch(argv, {})
            placements.update(identities)
            inputs[role] = _RoleGateInputs(argv, configuration)
        self._slots.require_open()
        self._inputs = MappingProxyType(inputs)

    @property
    def stream_limit(self) -> int:
        return self._stdio_limit

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self._inputs)

    @property
    def stopped(self) -> bool:
        return self._slots.stopped

    @property
    def gates(self) -> tuple[GatedProcess, ...]:
        return self._fence.gates

    @property
    def close_failed(self) -> bool:
        return self._fence.close_failed

    @property
    def failure(self) -> BoundGateFailure | None:
        return self._fence.failure

    def stop(self) -> None:
        if _current_loop() is not self._loop:
            raise ChannelError(ChannelFailure.CLOSED)
        self._fence.stop()

    async def start_role_stdio(self, role: str) -> StdioStreams:
        gate = self.start(role)
        await gate.prepare()
        await gate.acknowledge()
        return gate.borrow_stdio()

    def admit_root_turn(self) -> None:
        if _current_loop() is not self._loop or self.stopped:
            raise ChannelError(ChannelFailure.CLOSED)
        if self._clock.monotonic_ns() >= self._cutoff_ns:
            raise ChannelError(ChannelFailure.DEADLINE)
        if self._root_turn_admitted:
            raise ChannelError(ChannelFailure.BUSY)
        self._root_turn_admitted = True

    def start(self, role: str) -> BoundNativeGate:
        if _current_loop() is not self._loop:
            raise ChannelError(ChannelFailure.CLOSED)
        claim = self._slots.claim(role)
        inputs = self._inputs[role]

        def preflight() -> None:
            self._checkout.verify(self._checkout.path, self._cutoff_ns, self._clock)
            inputs.configuration.verify(
                inputs.configuration.directories, self._cutoff_ns, self._clock
            )

        def acquire(environment: dict[str, str]) -> BoundNativeGate:
            launch = self._process_launch(inputs.argv, environment)
            operation_id = new_operation_id()
            self._slots.require_open()
            gate = start_gated(
                launch,
                operation_id,
                self._checkout.anchor.descriptor,
                self._cutoff_ns,
                self._clock,
                stdio_limit=self._stdio_limit,
            )
            self._fence.register(gate)
            gate.require_acquired()
            self._slots.require_open()
            return BoundNativeGate(
                gate, self._fence, self._clock, self._cutoff_ns, self._loop
            )

        try:
            return self._slots.start(
                role,
                claim,
                acquire,
                preflight=preflight,
                on_failure=self._fail_acquisition,
            )
        finally:
            if self._slots.stopped:
                self.stop()

    def _fail_acquisition(self, error: BaseException) -> None:
        self._fence.record_failure(_acquisition_cause(error))

    def _process_launch(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> ProcessLaunch:
        try:
            return ProcessLaunch(
                executable=str(self._executable.path),
                argv=argv,
                environment=tuple(
                    EnvironmentVariable(name=name, value=value)
                    for name, value in environment.items()
                ),
                working_directory_resource_id=self._directory_id,
                stdin=None,
                stdout=None,
                stderr=None,
                domain=self._domain,
            )
        except ValidationError:
            pass
        raise _binding_refused()


def _acquisition_cause(error: BaseException) -> BoundGateFailure:
    if isinstance(
        error, (BoundCheckoutError, BoundRoleConfigurationError, ChannelError)
    ):
        return error.reason
    if isinstance(error, ProcessError):
        return error.condition
    if isinstance(error, SecretRevealRefused):
        return GateAcquisitionFailure.GRANT_UNAVAILABLE
    if isinstance(error, BoundSlotDeadline):
        return ChannelFailure.DEADLINE
    if isinstance(error, KoraProtocolError):
        return GateAcquisitionFailure.REJECTED
    if isinstance(error, OSError):
        return GateAcquisitionFailure.OS_ERROR
    if isinstance(error, asyncio.CancelledError):
        return GatePhaseFailure.CANCELLED
    return GatePhaseFailure.UNEXPECTED


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _binding_refused() -> KoraProtocolError:
    return KoraProtocolError(
        "launch_rejected", "the bound native launch inputs are invalid", False, {}
    )
