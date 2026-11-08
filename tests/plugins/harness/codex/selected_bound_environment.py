import asyncio
import os
import shutil
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
from kora.plugins.runtime.local.bound_checkout import (
    BoundCheckout,
    BoundCheckoutError,
    CheckoutFailure,
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
from kora.runner.installed_executable import (
    ExecutableFailure,
    ExecutablePin,
    ExecutableVerificationError,
    InstalledExecutable,
    verify_installed_executable,
)
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
    "executable-pin",
    "executable-path",
    "checkout-before-exec",
    "checkout-before-app",
]
BoundRole = Literal["app", "exec"]
SLOT_REFUSALS = frozenset({"duplicate", "stopped"})
ENVIRONMENT_REFUSALS = frozenset({"path", "grant", "reserved"})
INSTALLATION_REFUSALS: Mapping[str, ExecutableFailure] = MappingProxyType(
    {
        "executable-pin": ExecutableFailure.DIGEST,
        "executable-path": ExecutableFailure.PROTECTION,
    }
)
CHECKOUT_REFUSALS: Mapping[str, BoundRole] = MappingProxyType(
    {
        "checkout-before-exec": "exec",
        "checkout-before-app": "app",
    }
)
SELECTED_EXECUTABLE_PIN = ExecutablePin(
    319_346_888, "b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8"
)
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
    executable: InstalledExecutable
    checkout: BoundCheckout
    clock: SystemClock
    cutoff_ns: int
    expected_app: dict[str, str]
    expected_executor: dict[str, str]

    def start(self, role: BoundRole, frame_limit: int) -> SelectedGatedApp:
        recipe = self.inputs.app_server if role == "app" else self.inputs.exec_server
        argv = (str(self.executable.path), *recipe.argv[1:])

        def preflight() -> None:
            try:
                self.checkout.verify(recipe.cwd, self.cutoff_ns, self.clock)
            except BoundCheckoutError:
                self.fence.stop()
                raise

        def acquire(environment: dict[str, str]) -> SelectedGatedApp:
            return SelectedGatedApp(
                argv,
                self.checkout.anchor,
                environment,
                self.clock,
                self.cutoff_ns,
                frame_limit,
                register=self.fence.register,
            )

        return self.slots.start(
            role, self.slots.claim(role), acquire, preflight=preflight
        )

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
        self.checkout.close()


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
    original_checkout: Path | None = field(default=None, init=False)
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
        checkout_refusal = CHECKOUT_REFUSALS.get(self.case) == role
        if checkout_refusal:
            self.replace_checkout(launch.checkout)
        try:
            started = launch.start(role, frame_limit)
            if checkout_refusal:
                raise BoundEnvironmentError("selected_bound_checkout_not_refused")
            return started
        except BoundCheckoutError as error:
            if not checkout_refusal or error.reason is not CheckoutFailure.IDENTITY:
                raise BoundEnvironmentError("selected_bound_checkout_failed") from None
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

    def replace_checkout(self, checkout: BoundCheckout) -> None:
        workspace = checkout.path
        original = workspace.with_name("original-checkout")
        try:
            workspace.rename(original)
            workspace.mkdir(mode=0o700)
            original_entry = original.stat()
            replacement = workspace.stat()
        except OSError:
            raise BoundEnvironmentError(
                "selected_bound_checkout_control_failed"
            ) from None
        selected = checkout.anchor.identity
        expected = (selected.device, selected.inode)
        if (original_entry.st_dev, original_entry.st_ino) != expected or (
            replacement.st_dev,
            replacement.st_ino,
        ) == expected:
            raise BoundEnvironmentError("selected_bound_checkout_control_identity")
        self.original_checkout = original

    def verify_executable(
        self, binary: Path, cutoff_ns: int, clock: SystemClock
    ) -> InstalledExecutable | None:
        expected_failure = INSTALLATION_REFUSALS.get(self.case)
        pin = SELECTED_EXECUTABLE_PIN
        if self.case == "executable-pin":
            pin = ExecutablePin(pin.size, "0" * 64)
        try:
            verified = verify_installed_executable(binary, pin, cutoff_ns, clock)
        except ExecutableVerificationError as error:
            if error.reason is expected_failure:
                return None
            raise BoundEnvironmentError(
                "selected_bound_unexpected_executable_refusal"
            ) from None
        if expected_failure is not None:
            raise BoundEnvironmentError("selected_bound_executable_not_refused")
        return verified

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
        if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (65_532,) * 4:
            raise BoundEnvironmentError("selected_bound_unexpected_identity")
        self._cwd = str(workspace)
        clock = SystemClock()
        cutoff_ns = runner_deadline(deadline, clock)
        if self.case == "executable-path":
            binary = replaceable_executable(binary, workspace)
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
        if self.case in INSTALLATION_REFUSALS or self.case == "checkout-before-exec":
            self.attempt_scope.release()
        if (
            self.case in ENVIRONMENT_REFUSALS
            or self.case in INSTALLATION_REFUSALS
            or self.case in CHECKOUT_REFUSALS
        ):
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
        except SecretRevealRefused:
            raise BoundEnvironmentError("selected_bound_preflight_reveal") from None
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
        executable = self.verify_executable(binary, cutoff_ns, clock)
        if executable is None:
            return None
        if any(
            recipe.argv[0] != str(executable.path)
            for recipe in (inputs.app_server, inputs.exec_server)
        ):
            raise BoundEnvironmentError("selected_bound_executable_recipe_mismatch")
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
        try:
            checkout = BoundCheckout.capture(workspace)
        except BoundCheckoutError:
            raise BoundEnvironmentError(
                "selected_bound_checkout_capture_failed"
            ) from None
        return BoundLaunch(
            inputs,
            slots,
            BoundGateFence(slots),
            executable,
            checkout,
            clock,
            cutoff_ns,
            {**common, **inputs.app_server.environment, PROVIDER_KEY: key},
            {**common, **inputs.exec_server.environment},
        )


def replaceable_executable(binary: Path, workspace: Path) -> Path:
    staged = workspace / "executable-copy"
    selected = workspace / "candidate-executable"
    try:
        shutil.copyfile(binary, staged)
        staged.chmod(0o755)
        staged.rename(selected)
        if selected.stat().st_size != SELECTED_EXECUTABLE_PIN.size:
            raise BoundEnvironmentError("selected_bound_executable_control_size")
    except OSError:
        raise BoundEnvironmentError(
            "selected_bound_executable_control_failed"
        ) from None
    return selected
