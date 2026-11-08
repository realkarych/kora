import asyncio
import os
import resource
import shutil
import signal
import socket
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from kora.plugins.harness.codex.launch_configuration import (
    PROVIDER_KEY,
    CodexLaunchInputs,
    LaunchConfigurationError,
    LaunchConfigurationFailure,
    NativeProcessInputs,
)
from kora.plugins.runtime.local.bound_checkout import (
    BoundCheckout,
    BoundCheckoutError,
    CheckoutFailure,
)
from kora.plugins.runtime.local.bound_gate_fence import (
    GateAcquisitionFailure,
    GatePhaseFailure,
)
from kora.plugins.runtime.local.bound_native_context import BoundNativeLaunchContext
from kora.plugins.runtime.local.bound_native_launch import (
    BoundNativeLaunch,
    BoundNativeRole,
)
from kora.plugins.runtime.local.bound_role_configuration import (
    CONFIG_NAME,
    BoundRoleConfiguration,
    BoundRoleConfigurationError,
    RoleConfigurationFailure,
    RoleDirectories,
)
from kora.plugins.runtime.local.environment import (
    CandidateRoleEnvironment,
    bind_candidate_environments,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    LaunchContext,
    LaunchInputs,
    NativeLaunch,
    NativeLaunchContext,
)
from kora.protocols.lifetime import ProcessDomain
from kora.protocols.secret_value import SecretRevealRefused, SecretScope, SecretValue
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.installed_executable import (
    ExecutableFailure,
    ExecutablePin,
    ExecutableVerificationError,
    InstalledExecutable,
    verify_installed_executable,
)
from kora.runner.processes import GatedProcess, ProcessCondition, ProcessError
from tests.plugins.harness.codex.selected_gated_stdio import (
    GateBoundary,
    SelectedGatedApp,
    close_gated,
    parent_sockets,
    runner_deadline,
)
from tests.plugins.harness.codex.selected_launch import prepare_native_inputs

BoundCase = Literal[
    "ready",
    "context-ready",
    "context-legacy-live",
    "context-legacy-released",
    "context-cancel",
    "path",
    "grant",
    "reserved",
    "stopped",
    "partial",
    "acquisition",
    "handoff-expiry",
    "phase-failure",
    "phase-cancellation",
    "phase-entry-expiry",
    "phase-resume-expiry",
    "before-activation",
    "before-delivery",
    "executable-pin",
    "executable-path",
    "checkout-before-exec",
    "checkout-before-app",
    "bash-live",
    "bash-pin",
    "configuration-ready",
    "configuration-exec-edit",
    "configuration-app-file",
    "configuration-app-placement",
]
BoundRole = Literal["app", "exec"]
CONTEXT_LEGACY_CASES = frozenset({"context-legacy-live", "context-legacy-released"})
CONTEXT_CASES = CONTEXT_LEGACY_CASES | {"context-ready", "context-cancel"}
PHASE_REFUSALS = frozenset(
    {"phase-failure", "phase-cancellation", "phase-entry-expiry", "phase-resume-expiry"}
)
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
CONFIGURATION_REFUSALS: Mapping[str, tuple[BoundRole, RoleConfigurationFailure]] = (
    MappingProxyType(
        {
            "configuration-exec-edit": ("exec", RoleConfigurationFailure.BYTES),
            "configuration-app-file": ("app", RoleConfigurationFailure.FILE),
            "configuration-app-placement": ("app", RoleConfigurationFailure.PLACEMENT),
        }
    )
)
SELECTED_EXECUTABLE_PIN = ExecutablePin(
    319_346_888, "b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8"
)
BOUND_BASH = Path("/usr/bin/bash")
BOUND_BASH_PIN = ExecutablePin(
    1_265_648, "55b89ab22bee4792a210f493a53fb066accd5d30b69837c28d98be5ff863efcf"
)
ATTEMPT_CANARY = "bound-environment-attempt-token"
TEXT_NAME = "KORA_BOUND_AUTHORED_TEXT"
TEXT_VALUE = "bound-environment-authored-text"
WORKLOAD_HANDLE_NAME = "KORA_BOUND_AUTHORED_SECRET"
WORKLOAD_HANDLE_VALUE = "bound-environment-authored-secret"


class BoundEnvironmentError(ValueError):
    pass


class SpawnClock(SystemClock):
    def __init__(self) -> None:
        self.observed_cutoff: int | None = None
        self.activation_release: tuple[frozenset[str], int] | None = None

    def monotonic_ns(self) -> int:
        if self.activation_release is not None and self.observed_cutoff is None:
            endpoints, cutoff = self.activation_release
            if len(endpoints - parent_sockets()) == 1:
                self.observed_cutoff = cutoff
        if self.observed_cutoff is not None:
            return self.observed_cutoff
        return super().monotonic_ns()


@dataclass(frozen=True, repr=False)
class BoundLaunch:
    inputs: CodexLaunchInputs
    native: BoundNativeLaunch
    executable: InstalledExecutable
    bash: InstalledExecutable
    checkout: BoundCheckout
    clock: SystemClock
    expected_app: dict[str, str]
    expected_executor: dict[str, str]
    context: LaunchContext | None

    async def close(
        self,
        deadline: float,
        wrappers: tuple[SelectedGatedApp | None, ...],
        *,
        natural_app_refusal: bool,
    ) -> None:
        cutoff = runner_deadline(deadline, self.clock)
        gates = self.native.gates
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
        if self.native.close_failed or any(not gate.closed for gate in gates):
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
            launch.native.stop()
            if not exec_writer.is_closing():
                raise BoundEnvironmentError("selected_bound_exec_writer_not_fenced")
            reached = True

    try:
        await app.prepare(stop_at)
    except ChannelError as error:
        if reached and error.reason is ChannelFailure.CLOSED:
            return
        raise
    raise BoundEnvironmentError("selected_bound_expected_fence_refusal")


@dataclass(repr=False)
class BoundEnvironmentProbe:
    case: BoundCase
    attempt_scope: SecretScope
    app_scope: SecretScope
    retention_scope: SecretScope
    starts: int = field(default=0, init=False)
    unix_sockets: int = field(default=0, init=False)
    cwd_opens: int = field(default=0, init=False)
    original_checkout: Path | None = field(default=None, init=False)
    bash_path: Path | None = field(default=None, init=False)
    _cwd: str | None = field(default=None, init=False)
    _clock: SpawnClock = field(default_factory=SpawnClock, init=False)
    _cutoff_ns: int | None = field(default=None, init=False)
    _pre_app_sockets: frozenset[str] = field(default=frozenset(), init=False)
    settled: bool = field(default=False, init=False)
    configuration_refused: RoleConfigurationFailure | None = field(
        default=None, init=False
    )
    _configuration_root: BoundCheckout | None = field(default=None, init=False)
    _checkout: BoundCheckout | None = field(default=None, init=False)
    _configurations: dict[BoundRole, BoundRoleConfiguration] = field(
        default_factory=dict[BoundRole, BoundRoleConfiguration], init=False
    )

    @property
    def workspace_evidence(self) -> bool:
        return (
            self.case
            in {"configuration-ready", "partial", "acquisition", "handoff-expiry"}
            or self.case in CONFIGURATION_REFUSALS
            or self.phase_refusal
            or self.case in CONTEXT_CASES
        )

    @property
    def context_legacy(self) -> bool:
        return self.case in CONTEXT_LEGACY_CASES

    def role_name(self, role: BoundRole) -> str:
        return f"{role}-server" if self.case in CONTEXT_CASES else role

    @property
    def phase_refusal(self) -> bool:
        return self.case in PHASE_REFUSALS

    @property
    def late_configuration_case(self) -> bool:
        return self.case in ("configuration-app-file", "configuration-app-placement")

    def install_witness(self) -> None:
        sys.addaudithook(self.observe_acquisition)

    def observe_acquisition(self, event: str, args: tuple[object, ...]) -> None:
        if event == "subprocess.Popen":
            self.starts += 1
            if self.case == "handoff-expiry" and self.starts == 2:
                self._clock.observed_cutoff = self._cutoff_ns
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

    def verify_live_contenders(
        self, launch: BoundLaunch, writer: asyncio.StreamWriter
    ) -> None:
        for role in ("unknown-role", "exec"):
            try:
                launch.native.start(role)
            except KoraProtocolError as error:
                if not slot_refusal(error):
                    raise BoundEnvironmentError(
                        "selected_bound_live_contender_failed"
                    ) from None
            else:
                raise BoundEnvironmentError("selected_bound_live_contender_acquired")
            if writer.is_closing() or len(launch.native.gates) != 1:
                raise BoundEnvironmentError("selected_bound_live_contender_fenced")

    def start_role(
        self, launch: BoundLaunch, role: BoundRole
    ) -> SelectedGatedApp | None:
        if self.case == "partial" and role == "app":
            self.app_scope.release()
        checkout_refusal = CHECKOUT_REFUSALS.get(self.case) == role
        if checkout_refusal:
            self.replace_checkout(launch.checkout)
        configuration_refusal = CONFIGURATION_REFUSALS.get(self.case)
        expected_configuration = (
            configuration_refusal[1]
            if configuration_refusal is not None and configuration_refusal[0] == role
            else None
        )
        if expected_configuration is not None:
            self.change_configuration(launch, role)
        if self.case == "phase-resume-expiry" and role == "app":
            self._pre_app_sockets = parent_sockets()
        acquisition_control = self.case == "acquisition" and role == "app"
        expiry_control = self.case == "handoff-expiry" and role == "app"
        try:
            with prohibit_spawn() if acquisition_control else nullcontext():
                gate = launch.native.start(role)
            if acquisition_control or expiry_control:
                raise BoundEnvironmentError("selected_bound_expected_spawn_refusal")
            started = SelectedGatedApp(gate, launch.clock)
            if expected_configuration is not None:
                raise BoundEnvironmentError("selected_bound_configuration_not_refused")
            if checkout_refusal:
                raise BoundEnvironmentError("selected_bound_checkout_not_refused")
            return started
        except BoundCheckoutError as error:
            if (
                not checkout_refusal
                or error.reason is not CheckoutFailure.IDENTITY
                or launch.native.failure is not error.reason
            ):
                raise BoundEnvironmentError("selected_bound_checkout_failed") from None
        except BoundRoleConfigurationError as error:
            if (
                error.reason is not expected_configuration
                or not launch.native.stopped
                or launch.native.failure is not error.reason
            ):
                raise BoundEnvironmentError(
                    "selected_bound_configuration_failed"
                ) from None
            self.configuration_refused = error.reason
        except KoraProtocolError as error:
            if expiry_control:
                gates = launch.native.gates
                if (
                    not slot_refusal(error)
                    or self._clock.observed_cutoff is None
                    or not launch.native.stopped
                    or launch.native.failure is not ChannelFailure.DEADLINE
                    or len(gates) != 2
                    or any(gate.registration is None for gate in gates)
                ):
                    raise BoundEnvironmentError(
                        "selected_bound_handoff_expiry_failed"
                    ) from None
            elif self.case != "stopped" or role != "exec" or not slot_refusal(error):
                raise BoundEnvironmentError("selected_bound_slot_failed") from None
        except ProcessError as error:
            gates = launch.native.gates
            if (
                not acquisition_control
                or error.condition is not ProcessCondition.START_FAILED
                or not launch.native.stopped
                or launch.native.failure is not error.condition
                or len(gates) != 2
                or gates[0].registration is None
                or gates[1].registration is not None
                or not gates[1].closed
            ):
                raise BoundEnvironmentError(
                    "selected_bound_unexpected_spawn_refusal"
                ) from None
        except SecretRevealRefused:
            if (
                self.case != "partial"
                or role != "app"
                or launch.native.failure is not GateAcquisitionFailure.GRANT_UNAVAILABLE
            ):
                raise BoundEnvironmentError(
                    "selected_bound_secret_reveal_refused"
                ) from None
        finally:
            self._clock.observed_cutoff = None
        return None

    async def verify_legacy_context_refusal(self, launch: BoundLaunch) -> None:
        context = launch.context
        if context is None or not isinstance(context, NativeLaunchContext):
            raise BoundEnvironmentError("selected_native_context_missing")
        released = self.case == "context-legacy-released"
        if released:
            self.app_scope.release()
            self.attempt_scope.release()
        try:
            await context.allocate(
                LaunchInputs(
                    {"CODEX_API_KEY": "forbidden-fixture-input"},
                    launch.checkout.path / "untrusted-cwd",
                )
            )
        except KoraProtocolError as error:
            if (
                error.code != "launch_rejected"
                or error.message != "this invocation requires bound native start"
                or error.retryable
                or error.details
                or error.__cause__ is not None
                or error.__context__ is not None
            ):
                raise BoundEnvironmentError(
                    "selected_native_context_legacy_error"
                ) from None
        else:
            raise BoundEnvironmentError("selected_native_context_legacy_accepted")
        if (
            launch.native.gates
            or launch.native.stopped
            or launch.native.failure is not None
            or self.app_scope.released != released
            or self.attempt_scope.released != released
        ):
            raise BoundEnvironmentError("selected_native_context_legacy_effect")

    async def start_context_role(
        self, launch: BoundLaunch, role: BoundRole
    ) -> tuple[NativeLaunch, SelectedGatedApp]:
        context = launch.context
        if context is None or not isinstance(context, NativeLaunchContext):
            raise BoundEnvironmentError("selected_native_context_missing")
        previous = parent_sockets()
        gates = launch.native.gates
        selector = self.role_name(role)
        if role == "exec" and self.case == "context-ready":
            accepted = await start_contended_context_role(context, selector)
            try:
                await context.start_bound("app")
            except KoraProtocolError as error:
                if not slot_refusal(error):
                    raise BoundEnvironmentError(
                        "selected_native_context_role_error"
                    ) from None
            else:
                raise BoundEnvironmentError(
                    "selected_native_context_wrong_role_started"
                )
        else:
            accepted = await context.start_bound(selector)
        owned = launch.native.gates
        if launch.native.stopped or len(owned) != len(gates) + 1 or owned[:-1] != gates:
            raise BoundEnvironmentError("selected_native_context_gate_owner")
        return accepted, SelectedGatedApp.observe_started(
            owned[-1], launch.clock, previous
        )

    async def cancel_context_app(
        self, launch: BoundLaunch, executor: SelectedGatedApp, borrowed: NativeLaunch
    ) -> None:
        context = launch.context
        if context is None or not isinstance(context, NativeLaunchContext):
            raise BoundEnvironmentError("selected_native_context_missing")
        entered = asyncio.Event()

        async def start() -> NativeLaunch:
            entered.set()
            return await context.start_bound(self.role_name("app"))

        pending = asyncio.create_task(start())
        try:
            await entered.wait()
            gates = launch.native.gates
            if (
                pending.done()
                or len(gates) != 2
                or any(gate.registration is None for gate in gates)
            ):
                raise BoundEnvironmentError("selected_native_context_app_not_pending")
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if (
            not pending.cancelled()
            or launch.native.failure is not GatePhaseFailure.CANCELLED
        ):
            raise BoundEnvironmentError("selected_native_context_cancel_not_retained")
        try:
            await borrowed.writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            executor.verify_revoked()
            return
        raise BoundEnvironmentError("selected_native_context_stream_not_revoked")

    async def verify_phase_refusal(
        self,
        launch: BoundLaunch,
        app: SelectedGatedApp,
        exec_writer: asyncio.StreamWriter,
    ) -> None:
        gates = launch.native.gates
        cutoff = self._cutoff_ns
        if len(gates) != 2 or cutoff is None or exec_writer.is_closing():
            raise BoundEnvironmentError("selected_bound_phase_control_unavailable")
        if self.case == "phase-failure":
            await refuse_dead_prepare(gates[-1], app, cutoff)
            expected = ChannelFailure.CLOSED
        elif self.case == "phase-cancellation":
            await cancel_pending_prepare(gates[-1], app, exec_writer, launch.native)
            expected = GatePhaseFailure.CANCELLED
        else:
            await self.refuse_expired_phase(app, cutoff)
            expected = ChannelFailure.DEADLINE
        if (
            not launch.native.stopped
            or not exec_writer.is_closing()
            or launch.native.failure is not expected
        ):
            raise BoundEnvironmentError("selected_bound_phase_writer_not_fenced")

    async def refuse_expired_phase(self, app: SelectedGatedApp, cutoff: int) -> None:
        released = self.case == "phase-resume-expiry"
        endpoints = (
            parent_sockets() - self._pre_app_sockets if released else frozenset[str]()
        )
        if released and len(endpoints) != 2:
            raise BoundEnvironmentError("selected_bound_phase_endpoint_count")

        def expire_at_release(stage: GateBoundary) -> None:
            if stage == "before-delivery":
                raise BoundEnvironmentError("selected_bound_phase_expiry_delivered")
            if not endpoints.issubset(parent_sockets()):
                raise BoundEnvironmentError("selected_bound_phase_endpoint_missing")
            self._clock.activation_release = (endpoints, cutoff)

        if not released:
            self._clock.observed_cutoff = cutoff
        try:
            await app.prepare(expire_at_release if released else None)
        except ChannelError as error:
            if (
                error.reason is ChannelFailure.DEADLINE
                and self._clock.observed_cutoff == cutoff
            ):
                return
            raise
        finally:
            self._clock.activation_release = None
            self._clock.observed_cutoff = None
        raise BoundEnvironmentError("selected_bound_phase_expiry_not_refused")

    def change_configuration(self, launch: BoundLaunch, role: BoundRole) -> None:
        if self.late_configuration_case:
            self.retention_scope.register(ATTEMPT_CANARY)
            self.retention_scope.register(WORKLOAD_HANDLE_VALUE)
        self.attempt_scope.release()
        self.app_scope.release()
        recipe = (
            launch.inputs.app_server if role == "app" else launch.inputs.exec_server
        )
        home = role_directories(recipe).codex_home
        config = home / CONFIG_NAME
        expected = recipe.config.encode()
        try:
            original_identity = entry_identity(config)
            if self.case == "configuration-exec-edit":
                with config.open("ab") as target:
                    target.write(b"\n")
                valid_control = (
                    entry_identity(config) == original_identity
                    and config.read_bytes() == expected + b"\n"
                )
            else:
                if self.case == "configuration-app-placement":
                    original_home_identity = entry_identity(home)
                    original_home = home.with_name("original-codex")
                    home.rename(original_home)
                    home.mkdir(mode=0o700)
                    valid_placement = (
                        entry_identity(original_home) == original_home_identity
                        and entry_identity(home) != original_home_identity
                    )
                    original = original_home / CONFIG_NAME
                else:
                    original = config.with_name("original-config.toml")
                    config.rename(original)
                    valid_placement = True
                with open(config, "xb", opener=private_config_file) as target:
                    target.write(expected)
                valid_control = (
                    valid_placement
                    and entry_identity(original) == original_identity
                    and entry_identity(config) != original_identity
                    and original.read_bytes() == config.read_bytes() == expected
                )
        except OSError:
            raise BoundEnvironmentError(
                "selected_bound_configuration_control_failed"
            ) from None
        if not valid_control:
            raise BoundEnvironmentError("selected_bound_configuration_control_identity")

    def close_inputs(self) -> None:
        failed = False
        if self._checkout is not None:
            try:
                self._checkout.close()
            except BoundCheckoutError:
                failed = True
        for configuration in self._configurations.values():
            try:
                configuration.close()
            except BoundRoleConfigurationError:
                failed = True
        if failed:
            raise BoundEnvironmentError("selected_bound_configuration_cleanup_failed")

    def remove_workspace(self, root: Path) -> None:
        owner = self._configuration_root
        if owner is None:
            shutil.rmtree(root)
            return
        try:
            identity = owner.anchor.identity
            if owner.path != root or entry_identity(root) != (
                identity.device,
                identity.inode,
            ):
                raise BoundEnvironmentError("selected_bound_workspace_identity")
            shutil.rmtree(root)
            if os.fstat(owner.anchor.descriptor).st_nlink != 0 or os.path.lexists(root):
                raise BoundEnvironmentError("selected_bound_workspace_still_present")
        finally:
            try:
                owner.close()
            except BoundCheckoutError:
                raise BoundEnvironmentError(
                    "selected_bound_workspace_anchor_close"
                ) from None

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

    def verify_bash(
        self, cutoff_ns: int, clock: SystemClock
    ) -> InstalledExecutable | None:
        wrong_pin = self.case == "bash-pin"
        pin = (
            ExecutablePin(BOUND_BASH_PIN.size, "0" * 64)
            if wrong_pin
            else BOUND_BASH_PIN
        )
        try:
            bash = verify_installed_executable(BOUND_BASH, pin, cutoff_ns, clock)
        except ExecutableVerificationError as error:
            if wrong_pin and error.reason is ExecutableFailure.DIGEST:
                return None
            raise BoundEnvironmentError(
                "selected_bound_bash_verification_failed"
            ) from None
        if wrong_pin:
            raise BoundEnvironmentError("selected_bound_bash_not_refused")
        return bash

    def prepare(
        self,
        root: Path,
        workspace: Path,
        binary: Path,
        port: int,
        bearer: str,
        key: str,
        deadline: float,
        frame_limit: int,
    ) -> BoundLaunch | None:
        if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (65_532,) * 4:
            raise BoundEnvironmentError("selected_bound_unexpected_identity")
        self._cwd = str(workspace)
        clock = self._clock
        cutoff_ns = runner_deadline(deadline, clock)
        self._cutoff_ns = cutoff_ns
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
        if self.case in INSTALLATION_REFUSALS or self.case in {
            "checkout-before-exec",
            "bash-pin",
        }:
            self.attempt_scope.release()
        if (
            self.case in ENVIRONMENT_REFUSALS
            or self.case in INSTALLATION_REFUSALS
            or self.case in CHECKOUT_REFUSALS
            or self.case == "bash-pin"
        ):
            self.app_scope.release()
        bash = self.verify_bash(cutoff_ns, clock)
        if bash is None:
            return None
        self.bash_path = bash.path
        try:
            inputs = prepare_native_inputs(
                root,
                workspace,
                binary=binary,
                bash=bash.path,
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
            role_environments = {
                "app": CandidateRoleEnvironment(
                    inputs.app_server.environment, (PROVIDER_KEY,)
                ),
                "exec": CandidateRoleEnvironment(adapter, ()),
            }
            binding = bind_candidate_environments(
                cwd=workspace,
                attempt_token=attempt_token,
                workload=workload,
                secrets=authored,
                harness_credentials={PROVIDER_KEY: grant},
                roles={
                    self.role_name(role): role_environments[role]
                    for role, _ in native_roles(inputs)
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
        self.prepare_configurations(root, inputs, cutoff_ns, clock)
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
        self._checkout = checkout
        try:
            native = BoundNativeLaunch(
                binding,
                {
                    self.role_name(role): BoundNativeRole(
                        recipe.argv,
                        recipe.cwd,
                        role_environments[role],
                        self._configurations[role],
                    )
                    for role, recipe in native_roles(inputs)
                },
                executable=executable,
                checkout=checkout,
                directory_id="1" * 32,
                domain=ProcessDomain.DETACHED,
                stdio_limit=frame_limit,
                clock=clock,
                cutoff_ns=cutoff_ns,
            )
        except KoraProtocolError:
            raise BoundEnvironmentError(
                "selected_bound_native_binding_failed"
            ) from None
        if self.case == "stopped":
            native.stop()
            self.attempt_scope.release()
        return BoundLaunch(
            inputs,
            native,
            executable,
            bash,
            checkout,
            clock,
            {**common, **inputs.app_server.environment, PROVIDER_KEY: key},
            {**common, **inputs.exec_server.environment},
            BoundNativeLaunchContext(native, {})
            if self.case in CONTEXT_CASES
            else None,
        )

    def prepare_configurations(
        self, root: Path, inputs: CodexLaunchInputs, cutoff_ns: int, clock: Clock
    ) -> None:
        try:
            self._configuration_root = BoundCheckout.capture(root)
            for role, recipe in native_roles(inputs):
                directories = role_directories(recipe)
                configuration = BoundRoleConfiguration(
                    directories, recipe.config.encode()
                )
                self._configurations[role] = configuration
                configuration.prepare(self._configuration_root, cutoff_ns, clock)
        except (BoundCheckoutError, BoundRoleConfigurationError):
            raise BoundEnvironmentError(
                "selected_bound_configuration_capture_failed"
            ) from None


def native_roles(
    inputs: CodexLaunchInputs,
) -> tuple[tuple[BoundRole, NativeProcessInputs], ...]:
    return (("app", inputs.app_server), ("exec", inputs.exec_server))


@contextmanager
def prohibit_spawn() -> Generator[None]:
    original = resource.getrlimit(resource.RLIMIT_NPROC)
    resource.setrlimit(resource.RLIMIT_NPROC, (0, original[1]))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NPROC, original)


def slot_refusal(error: KoraProtocolError) -> bool:
    return (
        error.code == "launch_rejected"
        and error.message == "the bound native role is unavailable"
        and not error.retryable
        and not error.details
        and error.__cause__ is None
        and error.__context__ is None
    )


async def start_contended_context_role(
    context: NativeLaunchContext, role: str
) -> NativeLaunch:
    outcomes = await asyncio.gather(
        context.start_bound(role), context.start_bound(role), return_exceptions=True
    )
    starts = [result for result in outcomes if isinstance(result, NativeLaunch)]
    refused = [result for result in outcomes if isinstance(result, KoraProtocolError)]
    if len(starts) != 1 or len(refused) != 1 or not slot_refusal(refused[0]):
        raise BoundEnvironmentError("selected_native_context_competing_effect")
    return starts[0]


@contextmanager
def gate_pidfd(gate: GatedProcess) -> Generator[int]:
    if sys.platform != "linux":
        raise BoundEnvironmentError("selected_bound_phase_requires_linux")
    gate.require_acquired()
    registration = gate.registration
    if registration is None:
        raise BoundEnvironmentError("selected_bound_phase_gate_unregistered")
    descriptor = os.pidfd_open(registration.process.pid)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


async def refuse_dead_prepare(
    gate: GatedProcess, app: SelectedGatedApp, cutoff: int
) -> None:
    if sys.platform != "linux":
        raise BoundEnvironmentError("selected_bound_phase_requires_linux")
    with gate_pidfd(gate) as descriptor:
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        await gate.wait_exit(cutoff)
    try:
        await app.prepare()
    except ChannelError as error:
        if error.reason is ChannelFailure.CLOSED:
            return
        raise
    raise BoundEnvironmentError("selected_bound_dead_prepare_delivered")


async def cancel_pending_prepare(
    gate: GatedProcess,
    app: SelectedGatedApp,
    exec_writer: asyncio.StreamWriter,
    owner: BoundNativeLaunch,
) -> None:
    if sys.platform != "linux":
        raise BoundEnvironmentError("selected_bound_phase_requires_linux")
    with gate_pidfd(gate) as descriptor:
        try:
            await suspend_registered_child(descriptor)
            await cancel_admitted_prepare(app, exec_writer, owner)
        finally:
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)


async def suspend_registered_child(descriptor: int) -> None:
    if sys.platform != "linux" or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise BoundEnvironmentError("selected_bound_stop_observation_unavailable")
    loop = asyncio.get_running_loop()
    changed = asyncio.Event()
    loop.add_signal_handler(signal.SIGCHLD, changed.set)
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
        while True:
            state = os.waitid(
                os.P_PIDFD,
                descriptor,
                os.WSTOPPED | os.WEXITED | os.WNOWAIT | os.WNOHANG,
            )
            if state is not None:
                if state.si_code != os.CLD_STOPPED:
                    raise BoundEnvironmentError("selected_bound_prepare_not_suspended")
                return
            await changed.wait()
            changed.clear()
    finally:
        loop.remove_signal_handler(signal.SIGCHLD)


async def cancel_admitted_prepare(
    app: SelectedGatedApp, exec_writer: asyncio.StreamWriter, owner: BoundNativeLaunch
) -> None:
    entered = asyncio.Event()

    async def prepare() -> None:
        entered.set()
        await app.prepare()

    pending = asyncio.create_task(prepare())
    try:
        await entered.wait()
        require_pending_prepare(pending, exec_writer, owner)
        try:
            await app.prepare()
        except ChannelError as error:
            if error.reason is not ChannelFailure.BUSY:
                raise
        else:
            raise BoundEnvironmentError("selected_bound_overlap_admitted")
        try:
            app.bound_gate.borrow_stdio()
        except ChannelError as error:
            if error.reason is not ChannelFailure.BUSY:
                raise
        else:
            raise BoundEnvironmentError("selected_bound_wrong_phase_delivered")
        require_pending_prepare(pending, exec_writer, owner)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    if not pending.cancelled():
        raise BoundEnvironmentError("selected_bound_prepare_not_cancelled")


def require_pending_prepare(
    pending: asyncio.Task[None],
    exec_writer: asyncio.StreamWriter,
    owner: BoundNativeLaunch,
) -> None:
    if pending.done() or owner.stopped or exec_writer.is_closing():
        raise BoundEnvironmentError("selected_bound_prepare_not_pending")


def role_directories(recipe: NativeProcessInputs) -> RoleDirectories:
    return RoleDirectories(
        Path(recipe.environment["HOME"]),
        Path(recipe.environment["CODEX_HOME"]),
        Path(recipe.environment["TMPDIR"]),
    )


def entry_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def private_config_file(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


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
