import asyncio
import os
import sys
from collections.abc import Generator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.bound_checkout import BoundCheckout
from kora.plugins.runtime.local.bound_native_launch import (
    BoundNativeGate,
    BoundNativeLaunch,
    BoundNativeRole,
)
from kora.plugins.runtime.local.bound_role_configuration import (
    BoundRoleConfiguration,
    RoleDirectories,
)
from kora.plugins.runtime.local.environment import (
    BoundCandidateEnvironments,
    CandidateRoleEnvironment,
    bind_candidate_environments,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.lifetime import ProcessDomain
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.installed_executable import InstalledExecutable
from tests.plugins.harness.codex.selected_gated_stdio import close_gated
from tests.plugins.runtime.local.frames_support import ORIGIN_NS, StepClock

__all__ = ["kora_capture"]

CUTOFF = ORIGIN_NS + 100


@dataclass
class NativeInputs:
    checkout: BoundCheckout
    executable: InstalledExecutable
    roles: dict[str, BoundNativeRole]
    clock: StepClock

    def bind(
        self,
        capture: Capture,
        *,
        cwd: Path | None = None,
        roles: Mapping[str, CandidateRoleEnvironment] | None = None,
        workload: Mapping[str, str] | None = None,
    ) -> BoundCandidateEnvironments:
        return bind_candidate_environments(
            cwd=self.checkout.path if cwd is None else cwd,
            attempt_token=capture.scope.admit("synthetic-attempt"),
            workload={} if workload is None else workload,
            secrets={},
            harness_credentials={"CODEX_API_KEY": capture.scope.admit("synthetic-key")},
            roles=(
                {role: recipe.environment for role, recipe in self.roles.items()}
                if roles is None
                else roles
            ),
        )

    def acquire_owner(
        self,
        binding: BoundCandidateEnvironments,
        roles: Mapping[str, BoundNativeRole],
        *,
        clock: Clock | None = None,
        cutoff_ns: int = CUTOFF,
    ) -> BoundNativeLaunch:
        return BoundNativeLaunch(
            binding,
            roles,
            executable=self.executable,
            checkout=self.checkout,
            directory_id="1" * 32,
            domain=ProcessDomain.DETACHED,
            stdio_limit=4096,
            clock=self.clock if clock is None else clock,
            cutoff_ns=cutoff_ns,
        )


@pytest.fixture
def native_inputs(tmp_path: Path) -> Generator[NativeInputs]:
    root_path = tmp_path.resolve()
    workspace = root_path / "workspace"
    workspace.mkdir(mode=0o700)
    clock = StepClock()
    executable_path = Path(sys.executable).resolve()
    metadata = executable_path.stat()
    executable = InstalledExecutable(executable_path, metadata.st_dev, metadata.st_ino)
    roles: dict[str, BoundNativeRole] = {}
    with ExitStack() as owned:
        root = BoundCheckout.capture(root_path)
        owned.callback(root.close)
        checkout = BoundCheckout.capture(workspace)
        owned.callback(checkout.close)
        for role, grants in (("app", ("CODEX_API_KEY",)), ("exec", ())):
            parent = root_path / role
            directories = RoleDirectories(
                parent / "home", parent / "codex", parent / "tmp"
            )
            configuration = BoundRoleConfiguration(directories, b"fixture = true\n")
            owned.callback(configuration.close)
            configuration.prepare(root, CUTOFF, clock)
            environment = CandidateRoleEnvironment(
                {
                    "HOME": str(directories.home),
                    "CODEX_HOME": str(directories.codex_home),
                    "TMPDIR": str(directories.temporary),
                    "NATIVE_FIXTURE": role,
                },
                grants,
            )
            roles[role] = BoundNativeRole(
                (str(executable_path), "-V"), workspace, environment, configuration
            )
        yield NativeInputs(checkout, executable, roles, clock)


def check_binding_refusal(error: KoraProtocolError) -> None:
    assert error.code == "launch_rejected"
    assert error.message == "the bound native launch inputs are invalid"
    assert not error.retryable
    assert error.details == {}
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "change", ["cwd", "assignment", "grants", "executable", "placement"]
)
@pytest.mark.asyncio
async def test_role_capture_refuses_mismatched_trusted_inputs_before_reveal(
    native_inputs: NativeInputs, kora_capture: Capture, change: str
) -> None:
    roles = native_inputs.roles.copy()
    app = roles["app"]
    if change == "cwd":
        binding = native_inputs.bind(kora_capture, cwd=app.cwd.with_name("other"))
    elif change == "grants":
        binding = native_inputs.bind(
            kora_capture,
            roles={
                "app": replace(app.environment, harness_grants=()),
                "exec": replace(
                    roles["exec"].environment, harness_grants=("CODEX_API_KEY",)
                ),
            },
        )
    else:
        binding = native_inputs.bind(kora_capture)
        if change == "assignment":
            roles["app"] = replace(
                app,
                environment=replace(
                    app.environment,
                    adapter={**app.environment.adapter, "NATIVE_FIXTURE": "other"},
                ),
            )
        elif change == "executable":
            roles["app"] = replace(app, argv=("/wrong-executable", "-V"))
        else:
            roles["app"] = replace(app, configuration=roles["exec"].configuration)
    kora_capture.scope.release()
    before = set(os.listdir("/dev/fd"))

    with pytest.raises(KoraProtocolError) as refused:
        native_inputs.acquire_owner(binding, roles)
    observed = set(os.listdir("/dev/fd"))

    check_binding_refusal(refused.value)
    assert observed == before


@pytest.mark.asyncio
async def test_materialized_input_validation_returns_fixed_error_and_fences(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    binding = native_inputs.bind(
        kora_capture, workload={"SECRET_TEXT": "fixture\x00value"}
    )
    requested = native_inputs.roles.copy()
    owner = native_inputs.acquire_owner(binding, requested)
    requested.clear()
    baseline = set(os.listdir("/dev/fd"))

    with pytest.raises(KoraProtocolError) as refused:
        owner.start("app")
    with pytest.raises(KoraProtocolError) as fenced:
        owner.start("exec")

    check_binding_refusal(refused.value)
    assert fenced.value.code == "launch_rejected"
    assert fenced.value.message == "the bound native role is unavailable"
    assert owner.gates == ()
    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_bound_gate_delivers_once_and_refuses_wrong_phases(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    roles = native_inputs.roles.copy()
    app = roles["app"]
    roles["app"] = replace(
        app, argv=(app.argv[0], "-c", "import sys; sys.stdin.buffer.read()")
    )
    binding = native_inputs.bind(kora_capture)
    clock = SystemClock()
    cutoff = clock.monotonic_ns() + 10_000_000_000
    owner = native_inputs.acquire_owner(binding, roles, clock=clock, cutoff_ns=cutoff)
    handle = owner.start("app")
    (gate,) = owner.gates
    try:
        with pytest.raises(ChannelError) as premature:
            handle.borrow_stdio()
        prepared = await handle.prepare()
        with pytest.raises(ChannelError) as repeated_prepare:
            await handle.prepare()
        await handle.acknowledge()
        with pytest.raises(ChannelError) as repeated_activation:
            await handle.acknowledge()
        _, writer = handle.borrow_stdio()
        with pytest.raises(ChannelError) as repeated_delivery:
            handle.borrow_stdio()
        foreign = await asyncio.to_thread(foreign_requests, owner, handle)
        writer_still_open = not writer.is_closing()
        remained_admitted = not owner.stopped
    finally:
        owner.stop()
        await close_gated(gate, clock.monotonic_ns() + 5_000_000_000, clock)

    assert prepared == gate.registration
    assert all(
        refusal.value.reason is ChannelFailure.BUSY
        for refusal in (
            premature,
            repeated_prepare,
            repeated_activation,
            repeated_delivery,
        )
    )
    assert foreign == (ChannelFailure.CLOSED,) * 3
    assert owner.gates == (gate,)
    assert writer_still_open
    assert remained_admitted
    assert handle.closed


def foreign_requests(
    owner: BoundNativeLaunch, handle: BoundNativeGate
) -> tuple[ChannelFailure, ...]:
    async def request() -> tuple[ChannelFailure, ...]:
        with pytest.raises(ChannelError) as start:
            owner.start("exec")
        with pytest.raises(ChannelError) as stop:
            owner.stop()
        with pytest.raises(ChannelError) as borrow:
            handle.borrow_stdio()
        return start.value.reason, stop.value.reason, borrow.value.reason

    return asyncio.run(request())


class ExpiryClock(SystemClock):
    def __init__(self) -> None:
        self.expired_at: int | None = None

    def monotonic_ns(self) -> int:
        return super().monotonic_ns() if self.expired_at is None else self.expired_at


@pytest.mark.asyncio
async def test_valid_bound_prepare_at_cutoff_fences_its_registered_gate(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    binding = native_inputs.bind(kora_capture)
    clock = ExpiryClock()
    cutoff = clock.monotonic_ns() + 10_000_000_000
    owner = native_inputs.acquire_owner(
        binding, native_inputs.roles, clock=clock, cutoff_ns=cutoff
    )
    handle = owner.start("app")
    (gate,) = owner.gates
    clock.expired_at = cutoff
    try:
        with pytest.raises(ChannelError) as wrong_phase:
            handle.borrow_stdio()
        stopped_by_contender = owner.stopped
        with pytest.raises(ChannelError) as expired:
            await handle.prepare()
        stopped_by_prepare = owner.stopped
        first_cause = owner.phase_failure
        owner.stop()
        with pytest.raises(ChannelError) as later:
            await handle.prepare()
        retained_cause = owner.phase_failure
    finally:
        clock.expired_at = None
        owner.stop()
        await close_gated(gate, clock.monotonic_ns() + 5_000_000_000, clock)

    assert wrong_phase.value.reason is ChannelFailure.BUSY
    assert expired.value.reason is ChannelFailure.DEADLINE
    assert not stopped_by_contender
    assert stopped_by_prepare
    assert later.value.reason is ChannelFailure.CLOSED
    assert first_cause is retained_cause is ChannelFailure.DEADLINE
    assert handle.closed
