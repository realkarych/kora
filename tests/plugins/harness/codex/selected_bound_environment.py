import asyncio
import os
import resource
import shutil
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
from kora.runner.processes import ProcessCondition, ProcessError
from tests.plugins.harness.codex.selected_gated_stdio import (
    GateBoundary,
    SelectedGatedApp,
    close_gated,
    runner_deadline,
)
from tests.plugins.harness.codex.selected_launch import prepare_native_inputs

BoundCase = Literal[
    "ready",
    "path",
    "grant",
    "reserved",
    "stopped",
    "partial",
    "acquisition",
    "handoff-expiry",
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

    def monotonic_ns(self) -> int:
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
    retention_scope: SecretScope
    starts: int = field(default=0, init=False)
    unix_sockets: int = field(default=0, init=False)
    cwd_opens: int = field(default=0, init=False)
    original_checkout: Path | None = field(default=None, init=False)
    bash_path: Path | None = field(default=None, init=False)
    _cwd: str | None = field(default=None, init=False)
    _clock: SpawnClock = field(default_factory=SpawnClock, init=False)
    _cutoff_ns: int | None = field(default=None, init=False)
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
        )

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
            if not checkout_refusal or error.reason is not CheckoutFailure.IDENTITY:
                raise BoundEnvironmentError("selected_bound_checkout_failed") from None
        except BoundRoleConfigurationError as error:
            if error.reason is not expected_configuration or not launch.native.stopped:
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
                or len(gates) != 2
                or gates[0].registration is None
                or gates[1].registration is not None
                or not gates[1].closed
            ):
                raise BoundEnvironmentError(
                    "selected_bound_unexpected_spawn_refusal"
                ) from None
        except SecretRevealRefused:
            if self.case != "partial" or role != "app":
                raise BoundEnvironmentError(
                    "selected_bound_secret_reveal_refused"
                ) from None
        finally:
            self._clock.observed_cutoff = None
        return None

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
                roles=role_environments,
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
                    role: BoundNativeRole(
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
