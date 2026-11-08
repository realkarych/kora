import asyncio
import os
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from kora.plugins.harness.codex.launch_configuration import (
    PROVIDER_KEY,
    CodexLaunchInputs,
    LaunchConfigurationError,
    LaunchConfigurationFailure,
)
from kora.plugins.runtime.local.bound_gate_fence import BoundGateFence
from kora.plugins.runtime.local.bound_slots import BoundRoleSlots
from kora.plugins.runtime.local.environment import (
    CandidateRoleEnvironment,
    bind_candidate_environments,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretRevealRefused, SecretScope, SecretValue
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.processes import ProcessCondition, ProcessError
from tests.plugins.harness.codex.selected_gated_stdio import (
    GateBoundary,
    SelectedGatedApp,
    close_gated,
    runner_deadline,
)
from tests.plugins.harness.codex.selected_launch import (
    prepare_native_inputs,
    write_native_configuration,
)

BoundCase = Literal[
    "ready",
    "path",
    "grant",
    "reserved",
    "duplicate",
    "stopped",
    "partial",
    "before-activation",
    "before-delivery",
]
BoundRole = Literal["app", "exec"]
SLOT_REFUSALS = frozenset({"duplicate", "stopped"})
ENVIRONMENT_REFUSALS = frozenset({"path", "grant", "reserved"})
ATTEMPT_CANARY = "bound-environment-attempt-token"
TEXT_NAME = "KORA_BOUND_AUTHORED_TEXT"
TEXT_VALUE = "bound-environment-authored-text"
WORKLOAD_HANDLE_NAME = "KORA_BOUND_AUTHORED_SECRET"
WORKLOAD_HANDLE_VALUE = "bound-environment-authored-secret"


class BoundEnvironmentError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class BoundLaunch:
    inputs: CodexLaunchInputs
    slots: BoundRoleSlots
    fence: BoundGateFence
    clock: SystemClock
    cutoff_ns: int
    expected_app: dict[str, str]
    expected_executor: dict[str, str]

    def start(self, role: BoundRole, frame_limit: int) -> SelectedGatedApp:
        recipe = self.inputs.app_server if role == "app" else self.inputs.exec_server

        def acquire(environment: dict[str, str]) -> SelectedGatedApp:
            return SelectedGatedApp(
                recipe.argv,
                recipe.cwd,
                environment,
                self.clock,
                self.cutoff_ns,
                frame_limit,
                register=self.fence.register,
            )

        return self.slots.start(role, self.slots.claim(role), acquire)

    async def close(
        self,
        deadline: float,
        wrappers: tuple[SelectedGatedApp | None, ...],
        *,
        natural_app_refusal: bool,
    ) -> None:
        cutoff = runner_deadline(deadline, self.clock)
        gates = self.fence.gates
        natural_gate = gates[-1] if natural_app_refusal and len(gates) == 2 else None
        outcomes = await asyncio.gather(
            *(
                close_gated(gate, cutoff, self.clock, natural=gate is natural_gate)
                for gate in gates
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        if natural_app_refusal and (
            natural_gate is None or natural_gate.returncode != 72
        ):
            raise BoundEnvironmentError("selected_bound_expected_natural_gate_exit")
        for wrapper in wrappers:
            if wrapper is not None:
                wrapper.verify_closed()
        if self.fence.close_failed:
            raise BoundEnvironmentError("selected_bound_gate_close_failed")


async def refuse_fenced_app(
    launch: BoundLaunch,
    app: SelectedGatedApp,
    exec_writer: asyncio.StreamWriter,
    boundary: GateBoundary,
) -> None:
    reached = False

    def stop_at(stage: GateBoundary) -> None:
        nonlocal reached
        if stage == boundary:
            launch.fence.stop()
            if not exec_writer.is_closing():
                raise BoundEnvironmentError("selected_bound_exec_writer_not_fenced")
            reached = True

    try:
        await app.prepare(stop_at)
    except ProcessError as error:
        if (
            reached
            and boundary == "before-activation"
            and error.condition is ProcessCondition.UNKNOWN
        ):
            return
        raise
    except ChannelError as error:
        if (
            reached
            and boundary == "before-delivery"
            and error.reason is ChannelFailure.CLOSED
        ):
            return
        raise
    raise BoundEnvironmentError("selected_bound_expected_fence_refusal")


@dataclass(repr=False)
class BoundEnvironmentProbe:
    case: BoundCase
    attempt_scope: SecretScope
    app_scope: SecretScope
    starts: int = field(default=0, init=False)
    unix_sockets: int = field(default=0, init=False)
    cwd_opens: int = field(default=0, init=False)
    _cwd: str | None = field(default=None, init=False)
    settled: bool = field(default=False, init=False)

    def install_witness(self) -> None:
        sys.addaudithook(self.observe_acquisition)

    def observe_acquisition(self, event: str, args: tuple[object, ...]) -> None:
        if event == "subprocess.Popen":
            self.starts += 1
        elif event == "socket.__new__" and len(args) == 4 and args[1] == socket.AF_UNIX:
            self.unix_sockets += 1
        elif (
            event == "open"
            and len(args) == 3
            and args[0] == self._cwd
            and isinstance(args[2], int)
            and args[2] & os.O_DIRECTORY
        ):
            self.cwd_opens += 1

    def start_role(
        self, launch: BoundLaunch, role: BoundRole, frame_limit: int
    ) -> SelectedGatedApp | None:
        if self.case == "partial" and role == "app":
            self.app_scope.release()
        try:
            return launch.start(role, frame_limit)
        except KoraProtocolError as error:
            if (
                self.case not in SLOT_REFUSALS
                or role != "exec"
                or error.code != "launch_rejected"
                or error.message != "the bound native role is unavailable"
                or error.retryable
                or error.details
                or error.__cause__ is not None
                or error.__context__ is not None
            ):
                raise BoundEnvironmentError("selected_bound_slot_failed") from None
        except SecretRevealRefused:
            if self.case != "partial" or role != "app":
                raise BoundEnvironmentError(
                    "selected_bound_secret_reveal_refused"
                ) from None
        return None

    def prepare(
        self,
        root: Path,
        workspace: Path,
        binary: Path,
        port: int,
        bearer: str,
        key: str,
        deadline: float,
    ) -> BoundLaunch | None:
        self._cwd = str(workspace)
        clock = SystemClock()
        cutoff_ns = runner_deadline(deadline, clock)
        workload: Mapping[str, str] = MappingProxyType({TEXT_NAME: TEXT_VALUE})
        secret_name = "BASH_ENV" if self.case == "reserved" else WORKLOAD_HANDLE_NAME
        authored_scope = (
            self.app_scope if self.case == "reserved" else self.attempt_scope
        )
        authored: Mapping[str, SecretValue] = MappingProxyType(
            {secret_name: authored_scope.admit(WORKLOAD_HANDLE_VALUE)}
        )
        attempt_token = self.attempt_scope.admit(ATTEMPT_CANARY)
        grant = self.app_scope.admit(key)
        if self.case in ENVIRONMENT_REFUSALS:
            self.app_scope.release()
        try:
            inputs = prepare_native_inputs(
                root,
                workspace,
                binary=binary,
                port=port,
                bearer=bearer,
                workload_names=workload.keys(),
                workload_secret_names=authored.keys(),
            )
            adapter = dict(inputs.exec_server.environment)
            if self.case == "path":
                adapter["PATH"] = "bound-environment-path-canary"
            if self.case == "grant":
                adapter[PROVIDER_KEY] = "bound-environment-grant-canary"
            binding = bind_candidate_environments(
                cwd=workspace,
                attempt_token=attempt_token,
                workload=workload,
                secrets=authored,
                harness_credentials={PROVIDER_KEY: grant},
                roles={
                    "app": CandidateRoleEnvironment(
                        inputs.app_server.environment, (PROVIDER_KEY,)
                    ),
                    "exec": CandidateRoleEnvironment(adapter, ()),
                },
            )
            if self.case in ENVIRONMENT_REFUSALS:
                raise BoundEnvironmentError("selected_bound_preflight_not_refused")
        except LaunchConfigurationError as error:
            if (
                self.case != "reserved"
                or error.reason is not LaunchConfigurationFailure.ENVIRONMENT_CONFLICT
            ):
                raise BoundEnvironmentError("selected_bound_recipe_failed") from None
            return None
        except KoraProtocolError as error:
            if (
                self.case not in {"path", "grant"}
                or error.code != "launch_rejected"
                or error.message != "the candidate environment has conflicting owners"
                or error.retryable
                or error.details
                or error.__cause__ is not None
                or error.__context__ is not None
            ):
                raise BoundEnvironmentError("selected_bound_ownership_failed") from None
            return None
        write_native_configuration(inputs.app_server)
        write_native_configuration(inputs.exec_server)
        slots = BoundRoleSlots(binding, clock, cutoff_ns)
        if self.case == "duplicate":
            slots.claim("exec")
        elif self.case == "stopped":
            slots.stop()
        if self.case in SLOT_REFUSALS:
            self.attempt_scope.release()
        common = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(workspace),
            "KORA_ATTEMPT_TOKEN": ATTEMPT_CANARY,
            TEXT_NAME: TEXT_VALUE,
            WORKLOAD_HANDLE_NAME: WORKLOAD_HANDLE_VALUE,
        }
        return BoundLaunch(
            inputs,
            slots,
            BoundGateFence(slots),
            clock,
            cutoff_ns,
            {**common, **inputs.app_server.environment, PROVIDER_KEY: key},
            {**common, **inputs.exec_server.environment},
        )
