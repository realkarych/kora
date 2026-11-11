import asyncio
import os
import socket
import sys
from collections.abc import Generator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
from typing import cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.harness import NoLaunches
from kora.plugins.harness.codex.app_server_rpc import RpcError, RpcFailure
from kora.plugins.harness.codex.selected_turn import BoundTurnRpc
from kora.plugins.runtime.local.bound_checkout import BoundCheckout, CheckoutFailure
from kora.plugins.runtime.local.bound_native_context import (
    BoundAppTurn,
    BoundNativeLaunchContext,
    start_bound_app_turn,
)
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
from kora.protocols.harness import (
    LaunchContext,
    LaunchInputs,
    NativeLaunch,
    NativeLaunchContext,
    Placement,
)
from kora.protocols.lifetime import ProcessDomain
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.installed_executable import InstalledExecutable
from tests.plugins.harness.codex.selected_gated_stdio import close_gated
from tests.plugins.harness.codex.wire_fixture import FRAME_BUDGET, future_deadline, wire
from tests.plugins.runtime.local.frames_support import (
    ORIGIN_NS,
    StepClock,
    TickingClock,
)

CUTOFF = ORIGIN_NS + 100
PROVIDER_ROLES = frozenset({"app", "exec"})
APP_ROLE = frozenset({"app"})


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
        attempt_marker: str = "synthetic-attempt",
    ) -> BoundCandidateEnvironments:
        return bind_candidate_environments(
            cwd=self.checkout.path if cwd is None else cwd,
            attempt_token=capture.scope.admit(attempt_marker),
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
        stdio_limit: int = 4096,
    ) -> BoundNativeLaunch:
        return BoundNativeLaunch(
            binding,
            roles,
            executable=self.executable,
            checkout=self.checkout,
            directory_id="1" * 32,
            domain=ProcessDomain.DETACHED,
            stdio_limit=stdio_limit,
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


def check_slot_refusal(error: KoraProtocolError) -> None:
    assert error.code == "launch_rejected"
    assert error.message == "the bound native role is unavailable"
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
        first_cause = owner.failure
        owner.stop()
        with pytest.raises(ChannelError) as later:
            await handle.prepare()
        retained_cause = owner.failure
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


def has_native_context(context: LaunchContext) -> bool:
    return isinstance(context, NativeLaunchContext)


class ForgedRole(str):
    def __hash__(self) -> int:
        raise AssertionError("the context hashed a forged role")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("the context compared a forged role")


@pytest.mark.asyncio
async def test_bound_context_rejects_legacy_and_malformed_roles_before_acquisition(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture), native_inputs.roles
    )
    input_path = native_inputs.checkout.path
    placements: dict[Placement, Path] = {"harness_input": input_path}
    context = BoundNativeLaunchContext(owner, placements, provider_roles=PROVIDER_ROLES)
    placements.clear()
    legacy = NoLaunches()

    with pytest.raises(KoraProtocolError) as legacy_refused:
        await context.allocate(LaunchInputs({"CODEX_API_KEY": "crafted"}, input_path))
    with pytest.raises(KoraProtocolError) as forged:
        await context.start_bound(ForgedRole("exec"))
    with pytest.raises(KoraProtocolError) as malformed:
        await context.start_bound(cast("str", ["exec"]))

    assert has_native_context(context)
    assert not has_native_context(legacy)
    assert dict(context.placements) == {"harness_input": input_path}
    assert legacy_refused.value.code == "launch_rejected"
    assert legacy_refused.value.message == "this invocation requires bound native start"
    check_binding_refusal(forged.value)
    check_binding_refusal(malformed.value)
    assert owner.gates == ()
    assert not owner.stopped
    assert not kora_capture.scope.released


@pytest.mark.asyncio
async def test_bound_context_maps_preflight_deadline_without_private_context(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    clock = TickingClock(1)
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture),
        native_inputs.roles,
        clock=clock,
        cutoff_ns=ORIGIN_NS + 6,
    )
    context = BoundNativeLaunchContext(owner, {}, provider_roles=PROVIDER_ROLES)

    with pytest.raises(KoraProtocolError) as expired:
        await context.start_bound("app")
    first_cause = owner.failure
    with pytest.raises(KoraProtocolError) as refused:
        await context.start_bound("exec")
    owner.stop()

    assert expired.value.code == "launch_failed"
    assert expired.value.message == "the bound native start reached its deadline"
    assert not expired.value.retryable
    assert expired.value.details == {}
    assert expired.value.__context__ is None
    assert expired.value.__cause__ is None
    assert refused.value.code == "launch_rejected"
    assert first_cause is owner.failure is CheckoutFailure.DEADLINE
    assert owner.stopped
    assert owner.gates == ()


@pytest.mark.asyncio
async def test_bound_context_keeps_expired_slot_refusal_without_consumption(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    clock = ExpiryClock()
    cutoff = clock.monotonic_ns() + 10_000_000_000
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture),
        native_inputs.roles,
        clock=clock,
        cutoff_ns=cutoff,
    )
    context = BoundNativeLaunchContext(owner, {}, provider_roles=PROVIDER_ROLES)
    clock.expired_at = cutoff

    with pytest.raises(KoraProtocolError) as refused:
        await context.start_bound("app")

    assert type(refused.value) is KoraProtocolError
    assert refused.value.code == "launch_rejected"
    assert refused.value.message == "the bound native role is unavailable"
    assert refused.value.__context__ is None
    assert owner.failure is None
    assert owner.gates == ()
    assert not owner.stopped


@pytest.mark.asyncio
async def test_bound_context_borrows_narrow_real_stdio(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    roles = native_inputs.roles.copy()
    app = roles["app"]
    script = (
        "import sys;"
        "data = sys.stdin.buffer.readline();"
        "sys.stdout.buffer.write(data);"
        "sys.stdout.buffer.flush();"
        "sys.stdin.buffer.read()"
    )
    roles["app"] = replace(app, argv=(app.argv[0], "-u", "-c", script))
    clock = SystemClock()
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture),
        roles,
        clock=clock,
        cutoff_ns=clock.monotonic_ns() + 10_000_000_000,
    )
    context = BoundNativeLaunchContext(owner, {}, provider_roles=PROVIDER_ROLES)
    try:
        async with asyncio.timeout(10):
            started = await context.start_bound("app")
            with pytest.raises(ValueError, match="bounded positive"):
                await started.reader.read(-1)
            with pytest.raises(ValueError, match="bounded positive"):
                await started.reader.read(owner.stream_limit + 1)
            started.writer.write(b"owned stdio\n")
            await started.writer.drain()
            head = await started.reader.read(1)
            tail = await started.reader.readline()
    finally:
        owner.stop()
        for gate in owner.gates:
            await close_gated(gate, clock.monotonic_ns() + 5_000_000_000, clock)

    assert head + tail == b"owned stdio\n"
    assert not hasattr(started.writer, "transport")
    assert not hasattr(started.writer, "get_extra_info")
    assert not hasattr(started.writer, "close")
    assert len(owner.gates) == 1
    assert owner.gates[0].closed


@pytest.mark.parametrize("roles", [frozenset[str](), frozenset({"app", "missing"})])
@pytest.mark.asyncio
async def test_bound_context_refuses_invalid_provider_roles(
    native_inputs: NativeInputs, kora_capture: Capture, roles: frozenset[str]
) -> None:
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture), native_inputs.roles
    )

    with pytest.raises(KoraProtocolError) as refused:
        BoundNativeLaunchContext(owner, {}, provider_roles=roles)

    check_binding_refusal(refused.value)
    assert owner.gates == ()
    assert not owner.stopped


@pytest.mark.asyncio
async def test_hidden_bound_role_refuses_before_secret_materialization(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture), native_inputs.roles
    )
    context = BoundNativeLaunchContext(owner, {}, provider_roles=APP_ROLE)
    kora_capture.scope.release()

    with pytest.raises(KoraProtocolError) as refused:
        await context.start_bound("exec")

    check_slot_refusal(refused.value)
    assert owner.gates == ()
    assert not owner.stopped
    assert owner.failure is None


@pytest.mark.asyncio
async def test_runtime_starts_bound_role_hidden_from_provider(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    clock = SystemClock()
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture),
        native_inputs.roles,
        clock=clock,
        cutoff_ns=clock.monotonic_ns() + 10_000_000_000,
    )
    context = BoundNativeLaunchContext(owner, {}, provider_roles=APP_ROLE)

    try:
        async with asyncio.timeout(10):
            with pytest.raises(KoraProtocolError) as refused:
                await context.start_bound("exec")
            before = owner.gates
            reader, writer = await owner.start_role_stdio("exec")
            result = await reader.readline()
    finally:
        owner.stop()
        for gate in owner.gates:
            await close_gated(gate, clock.monotonic_ns() + 5_000_000_000, clock)

    check_slot_refusal(refused.value)
    assert before == ()
    assert result.startswith(b"Python ")
    assert len(owner.gates) == 1
    assert owner.gates[0].closed
    assert writer.is_closing()


@pytest.mark.asyncio
async def test_bound_owner_fences_pending_private_stream_with_live_app(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    roles = native_inputs.roles.copy()
    app = roles["app"]
    roles["app"] = replace(
        app, argv=(app.argv[0], "-c", "import sys; sys.stdin.buffer.read()")
    )
    clock = SystemClock()
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture),
        roles,
        clock=clock,
        cutoff_ns=clock.monotonic_ns() + 10_000_000_000,
    )
    context = BoundNativeLaunchContext(owner, {}, provider_roles=APP_ROLE)
    entered = asyncio.Event()

    async def start_private() -> None:
        entered.set()
        await owner.start_role_stdio("exec")

    try:
        async with asyncio.timeout(10):
            app_stream = await context.start_bound("app")
            pending = asyncio.create_task(start_private())
            try:
                await entered.wait()
                registered = owner.gates
                assert len(registered) == 2
                assert not pending.done()
                assert all(gate.registration is not None for gate in registered)
                with pytest.raises(KoraProtocolError) as competing:
                    await owner.start_role_stdio("exec")
                remained_open = not owner.stopped and owner.gates == registered
                owner.stop()
                with pytest.raises(ChannelError) as fenced:
                    await pending
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
    finally:
        owner.stop()
        cleanup_cutoff = clock.monotonic_ns() + 5_000_000_000
        outcomes = await asyncio.gather(
            *(close_gated(gate, cleanup_cutoff, clock) for gate in owner.gates),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
    async with asyncio.timeout(5):
        app_eof = await app_stream.reader.read(1)

    check_slot_refusal(competing.value)
    assert remained_open
    assert fenced.value.reason is ChannelFailure.CLOSED
    assert owner.failure is None
    assert owner.gates == registered
    assert all(gate.closed for gate in registered)
    assert app_eof == b""


def root_rpc(app: BoundAppTurn) -> BoundTurnRpc:
    return BoundTurnRpc(
        app,
        clock=asyncio.get_running_loop().time,
        next_request_id=count(1).__next__,
        max_messages=16,
    )


@pytest.mark.asyncio
async def test_bound_root_app_spends_once_on_its_native_stream(
    native_inputs: NativeInputs, kora_capture: Capture
) -> None:
    app = native_inputs.roles["app"]
    script = (
        "import json,sys; "
        "request=json.loads(sys.stdin.buffer.readline()); "
        "print(json.dumps({'id':request['id'],'result':{}}),flush=True); "
        "sys.stdin.buffer.read()"
    )
    roles = {
        "app-server": replace(app, argv=(app.argv[0], "-u", "-c", script)),
        "exec-server": native_inputs.roles["exec"],
    }
    binding = native_inputs.bind(
        kora_capture, roles={role: recipe.environment for role, recipe in roles.items()}
    )
    clock = SystemClock()
    owner = native_inputs.acquire_owner(
        binding, roles, clock=clock, cutoff_ns=clock.monotonic_ns() + 10_000_000_000
    )
    context = BoundNativeLaunchContext(
        owner, {}, provider_roles=frozenset({"app-server"})
    )

    try:
        async with asyncio.timeout(10):
            turn = await start_bound_app_turn(owner, context.placements)
            with pytest.raises(ChannelError) as foreign:
                await asyncio.to_thread(turn.admit_root_turn)
            first = await root_rpc(turn).call(
                "turn/start", {}, deadline=future_deadline(), observe=lambda _: None
            )
            with pytest.raises(ChannelError) as duplicate:
                await root_rpc(turn).call(
                    "turn/start", {}, deadline=future_deadline(), observe=lambda _: None
                )
            still_open = not owner.stopped
    finally:
        owner.stop()
        for gate in owner.gates:
            await close_gated(gate, clock.monotonic_ns() + 5_000_000_000, clock)

    assert first == {}
    assert not hasattr(context, "start_app_turn")
    assert foreign.value.reason is ChannelFailure.CLOSED
    assert duplicate.value.reason is ChannelFailure.BUSY
    assert still_open
    assert owner.failure is None
    assert len(owner.gates) == 1
    assert owner.gates[0].closed


class RootWireWriter:
    def __init__(self, writer: asyncio.StreamWriter, *, raw_write: bool) -> None:
        self.writer = writer
        self.descriptor = cast(
            "socket.socket", writer.get_extra_info("socket")
        ).fileno()
        self.raw_write = raw_write
        self.entries = 0
        self.draining = asyncio.Event()

    def write(self, data: bytes, /) -> None:
        self.entries += 1
        if self.raw_write:
            if os.write(self.descriptor, data) != len(data):
                raise OSError("root fixture write was partial")
        else:
            self.writer.write(data)

    async def drain(self) -> None:
        self.draining.set()
        await self.writer.drain()


@pytest.mark.parametrize("failure", ["write", "drain", "missing_ack"])
@pytest.mark.asyncio
async def test_root_credit_stays_spent_after_native_send_failure(
    native_inputs: NativeInputs, kora_capture: Capture, failure: str
) -> None:
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture), native_inputs.roles, stdio_limit=FRAME_BUDGET
    )
    async with wire() as connection:
        witness = RootWireWriter(connection.writer, raw_write=failure == "write")
        turn = BoundAppTurn(NativeLaunch(connection.reader, witness), owner)
        contender = root_rpc(turn)
        params: dict[str, object] = (
            {"prompt": "x" * 120_000} if failure == "drain" else {}
        )
        observed_frame = b""
        if failure == "write":
            connection.peer_writer.close()
            await connection.peer_writer.wait_closed()
            assert await connection.reader.read() == b""
        if failure == "drain":
            peer_transport = connection.peer_writer.transport
            assert isinstance(peer_transport, asyncio.ReadTransport)
            peer_transport.pause_reading()
            connection.writer.transport.set_write_buffer_limits(high=4096, low=1024)

        request = connection.start(
            root_rpc(turn).call(
                "turn/start", params, deadline=future_deadline(), observe=lambda _: None
            )
        )
        if failure == "drain":
            await witness.draining.wait()
            assert connection.writer.transport.get_write_buffer_size() > 4096
            connection.peer_writer.transport.abort()
        elif failure == "missing_ack":
            observed_frame = await connection.peer_reader.readline()
            with pytest.raises(ChannelError) as competing:
                await contender.call(
                    "turn/start", {}, deadline=future_deadline(), observe=lambda _: None
                )
            assert competing.value.reason is ChannelFailure.BUSY
            assert not request.done()
            connection.peer_writer.close()
        with pytest.raises(RpcError) as original:
            await request
        with pytest.raises(ChannelError) as replacement:
            await root_rpc(turn).call(
                "turn/start", {}, deadline=future_deadline(), observe=lambda _: None
            )

        assert original.value.reason is RpcFailure.DISCONNECTED
        assert replacement.value.reason is ChannelFailure.BUSY
        assert witness.entries == 1
        assert witness.draining.is_set() == (failure != "write")
        if failure == "missing_ack":
            assert observed_frame == b'{"id":1,"method":"turn/start","params":{}}\n'
        assert not owner.stopped
        assert owner.failure is None


@pytest.mark.parametrize("fence", ["deadline", "stop"])
@pytest.mark.asyncio
async def test_root_fence_refuses_before_writer_and_new_attempt_can_start(
    native_inputs: NativeInputs, kora_capture: Capture, fence: str
) -> None:
    clock = ExpiryClock()
    cutoff = clock.monotonic_ns() + 10_000_000_000
    owner = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture),
        native_inputs.roles,
        clock=clock,
        cutoff_ns=cutoff,
    )
    fresh = native_inputs.acquire_owner(
        native_inputs.bind(kora_capture, attempt_marker="synthetic-fresh-attempt"),
        native_inputs.roles,
    )
    async with wire() as connection:
        witness = RootWireWriter(connection.writer, raw_write=False)
        stream = NativeLaunch(connection.reader, witness)
        deadline = future_deadline()

        def fence_during_request() -> int:
            if fence == "deadline":
                clock.expired_at = cutoff
            else:
                owner.stop()
            return 1

        rpc = BoundTurnRpc(
            BoundAppTurn(stream, owner),
            clock=asyncio.get_running_loop().time,
            next_request_id=fence_during_request,
            max_messages=16,
        )

        with pytest.raises(ChannelError) as expired:
            await rpc.call("turn/start", {}, deadline=deadline, observe=lambda _: None)
        expired_entries = witness.entries
        fresh_turn = BoundAppTurn(stream, fresh)
        with pytest.raises(RpcError) as invalid:
            await root_rpc(fresh_turn).call(
                "turn/start",
                {"large": "x" * FRAME_BUDGET},
                deadline=deadline,
                observe=lambda _: None,
            )
        invalid_entries = witness.entries
        request = connection.start(
            root_rpc(fresh_turn).call(
                "turn/start", {}, deadline=deadline, observe=lambda _: None
            )
        )
        frame = await connection.peer_reader.readline()
        await connection.send(b'{"id":1,"result":{}}\n')
        result = await request

        expected_reason = (
            ChannelFailure.DEADLINE if fence == "deadline" else ChannelFailure.CLOSED
        )
        assert expired.value.reason is expected_reason
        assert expired_entries == invalid_entries == 0
        assert invalid.value.reason is RpcFailure.FRAME_LIMIT
        assert result == {}
        assert frame == b'{"id":1,"method":"turn/start","params":{}}\n'
        assert witness.entries == 1
