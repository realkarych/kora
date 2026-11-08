from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import ValidationError

from kora.plugins.runtime.local.bound_checkout import BoundCheckout
from kora.plugins.runtime.local.bound_gate_fence import BoundGateFence
from kora.plugins.runtime.local.bound_role_configuration import BoundRoleConfiguration
from kora.plugins.runtime.local.bound_slots import BoundRoleSlots
from kora.plugins.runtime.local.environment import (
    BoundCandidateEnvironments,
    CandidateRoleEnvironment,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ResourceId,
    new_operation_id,
)
from kora.protocols.time import Clock
from kora.runner.installed_executable import InstalledExecutable
from kora.runner.processes import GatedProcess, start_gated


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
        captured = dict(roles)
        if (
            not captured
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
    def stopped(self) -> bool:
        return self._slots.stopped

    @property
    def gates(self) -> tuple[GatedProcess, ...]:
        return self._fence.gates

    @property
    def close_failed(self) -> bool:
        return self._fence.close_failed

    def stop(self) -> None:
        self._fence.stop()

    def start(self, role: str) -> GatedProcess:
        claim = self._slots.claim(role)
        inputs = self._inputs[role]

        def preflight() -> None:
            self._checkout.verify(self._checkout.path, self._cutoff_ns, self._clock)
            inputs.configuration.verify(
                inputs.configuration.directories, self._cutoff_ns, self._clock
            )

        def acquire(environment: dict[str, str]) -> GatedProcess:
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
            return gate

        try:
            return self._slots.start(role, claim, acquire, preflight=preflight)
        finally:
            if self._slots.stopped:
                self.stop()

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


def _binding_refused() -> KoraProtocolError:
    return KoraProtocolError(
        "launch_rejected", "the bound native launch inputs are invalid", False, {}
    )
