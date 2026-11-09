import asyncio
import os
import secrets
import select
import socket
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from kora.plugins.harness.codex.app_server_rpc import AppServerRpc, RpcError
from kora.plugins.harness.codex.app_server_turn import (
    TurnError,
    observe_app_server_turn,
)
from kora.plugins.harness.codex.launch_configuration import ExecutorTransport
from kora.plugins.harness.codex.selected_startup import (
    SelectedEnvironment,
    prepare_selected_thread,
)
from tests.plugins.harness.codex import probe
from tests.plugins.harness.codex.guarded_executor import (
    MALFORMED_HEADER_CANARY,
    REFUSAL,
    ExecutorGuard,
    GuardAction,
    GuardFailure,
    GuardKind,
    HookOutcome,
    verify_malformed_header_refused,
    verify_non_upgrade_refused,
    verify_upgrade_refused,
)
from tests.plugins.harness.codex.selected_executor_peer import ExecutorPeerWitness
from tests.plugins.harness.codex.selected_launch import (
    HOOK_TIMEOUT_SOCKET,
    SELECTED_HOOK_CASES,
    SELECTED_MODEL,
    SelectedLaunchCase,
    materialize_native_inputs,
    prepare_native_inputs,
    role_homes,
    selected_command,
    selected_managed_hook,
)

Mode = Literal["allow", "deny", "error", "timeout", "staged-timeout"]
MAX_INITIAL_ENV_BYTES = 131_072


@dataclass(frozen=True, slots=True)
class Scenario:
    kind: GuardKind
    mode: Mode
    case: SelectedLaunchCase

    @property
    def command(self) -> str:
        return probe.PATCH if self.kind == "patch" else selected_command(self.case)

    @property
    def hook_outcome(self) -> HookOutcome:
        if self.case in SELECTED_HOOK_CASES:
            return probe.SELECTED_HOOK_OUTCOMES[self.case]
        return "completed", None

    @property
    def counts(self) -> tuple[int, int, int]:
        if self.mode == "allow":
            return (1, 1, 0)
        return (0, 0, 0) if self.mode == "deny" else (1, 0, 1)

    @property
    def timed_out(self) -> bool:
        return self.mode in {"timeout", "staged-timeout"}


SCENARIOS = {
    f"{scenario.kind}-{scenario.mode}": scenario
    for scenario in (
        Scenario("bash", "allow", "selected-allow"),
        Scenario("bash", "deny", "selected-hook-deny"),
        Scenario("bash", "error", "selected-hook-exit-one"),
        Scenario("bash", "timeout", "selected-hook-timeout"),
        Scenario("patch", "allow", "selected-patch-allow"),
        Scenario("patch", "deny", "selected-patch-deny"),
        Scenario("patch", "error", "selected-patch-exit-one"),
        Scenario("patch", "timeout", "selected-patch-timeout"),
        Scenario("patch", "staged-timeout", "selected-patch-timeout"),
    )
}


class GuardResponses(probe.ResponsesFixture):
    def __init__(self, scenario: Scenario) -> None:
        super().__init__(scenario.case, one_shot=True)
        self._scenario = scenario

    def respond(self, request: dict[str, object]) -> bytes:
        probe.require(request.get("model") == SELECTED_MODEL, "guard_model_mismatch")
        if self.requests != 1 or self._scenario.mode in {"allow", "deny"}:
            return super().respond(request)
        self.requests += 1
        probe.require(request.get("stream") is True, "executor_guard_requires_stream")
        if self._scenario.kind == "patch":
            lines = probe.tool_output(
                request, probe.CALL_ID, "custom_tool_call_output"
            ).splitlines()
            probe.require(
                bool(lines)
                and lines[0] == "Exit code: 1"
                and any(
                    line.startswith("Failed to write file ")
                    and line.endswith(f"/{probe.MARKER}")
                    for line in lines
                ),
                "executor_guard_missing_patch_refusal",
            )
        else:
            probe.require(
                REFUSAL in probe.tool_output(request, probe.CALL_ID),
                "executor_guard_missing_command_refusal",
            )
        return probe.FINAL_RESPONSE


async def spawn(
    argv: tuple[str, ...], workspace: Path, environment: dict[str, str]
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=workspace,
        env=environment,
        start_new_session=True,
        limit=probe.MAX_FRAME_BYTES,
    )


def verify_executor_key_absent(child: asyncio.subprocess.Process) -> None:
    pidfd = probe.open_child_pidfd(child.pid)
    try:
        with Path(f"/proc/{child.pid}/environ").open("rb") as source:
            contents = source.read(MAX_INITIAL_ENV_BYTES + 1)
        probe.require(
            0 < len(contents) <= MAX_INITIAL_ENV_BYTES
            and all(
                not entry.startswith(b"CODEX_API_KEY=")
                for entry in contents.split(b"\0")
            )
            and not select.select([pidfd], [], [], 0)[0],
            "guard_native_executor_key_boundary",
        )
    finally:
        os.close(pidfd)


def stdio(
    child: asyncio.subprocess.Process,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if child.stdout is None or child.stdin is None:
        raise GuardFailure("executor fixture has no owned stdio")
    return child.stdout, child.stdin


def hook_pid(scenario: Scenario) -> int:
    if scenario.kind == "patch":
        return probe.verify_patch_hook(selected_binding=True)
    return probe.verify_shell_hook(scenario.command, selected_binding=True)


async def witness_timeout(
    listener: socket.socket,
    scenario: Scenario,
    roles: tuple[asyncio.subprocess.Process, asyncio.subprocess.Process],
    guard: ExecutorGuard,
) -> None:
    if sys.platform != "linux":
        raise GuardFailure("executor fixture timeout witness requires Linux")
    loop = asyncio.get_running_loop()
    with ExitStack() as owned:
        role_descriptors: list[int] = []
        for role in roles:
            role_descriptor = probe.open_child_pidfd(role.pid)
            owned.callback(os.close, role_descriptor)
            role_descriptors.append(role_descriptor)
        peer, _ = await loop.sock_accept(listener)
        owned.enter_context(peer)
        peer.setblocking(False)
        probe.require(await loop.sock_recv(peer, 1) == b"1", "guard_hook_not_ready")
        credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid = hook_pid(scenario)
        probe.require(
            len(credentials) == 12
            and int.from_bytes(credentials[:4], sys.byteorder, signed=True) == pid,
            "guard_hook_wrong_peer",
        )
        descriptor = probe.open_child_pidfd(pid)
        owned.callback(os.close, descriptor)
        probe.require(
            not select.select([descriptor], [], [], 0)[0], "guard_hook_not_live"
        )
        if scenario.mode == "staged-timeout":
            await guard.wait_turn()
            probe.require(guard.permit_matches(), "guard_staged_permit_missing")
        await probe.wait_pidfd_exit(descriptor)
        probe.require(not await loop.sock_recv(peer, 1), "guard_hook_peer_open")
        probe.require(
            not select.select(role_descriptors, [], [], 0)[0],
            "guard_native_role_exited",
        )


def verify_marker(scenario: Scenario, root: Path, workspace: Path) -> None:
    marker = workspace / probe.MARKER
    if scenario.kind == "patch" or scenario.mode != "allow":
        probe.verify_patch_marker(marker, writes=scenario.mode == "allow")
        return
    bash = Path("/bin/bash").resolve(strict=True)
    expected = f"{bash}\n{role_homes(root / 'exec').home}\n".encode()
    probe.require(
        marker.is_file() and not marker.is_symlink(), "guard_bash_marker_missing"
    )
    with marker.open("rb") as stream:
        probe.require(
            stream.read(len(expected) + 1) == expected, "guard_bash_marker_mismatch"
        )


async def run_guard(root: Path, scenario: Scenario, *, witness_peer: bool) -> None:
    probe.CONTROL.mkdir(mode=0o700)
    (probe.CONTROL / "decision").write_text(scenario.case)
    (probe.CONTROL / "executor-guard").touch()
    if scenario.mode == "staged-timeout":
        (probe.CONTROL / "executor-guard-stage").touch()
    workspace = root / "workspace"
    workspace.mkdir()
    fixture = GuardResponses(scenario)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + probe.TURN_SECONDS

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fixture.accept(reader, writer, deadline)

    server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=16_384)
    app: asyncio.subprocess.Process | None = None
    backend: asyncio.subprocess.Process | None = None
    guard: ExecutorGuard | None = None
    peer: ExecutorPeerWitness | None = None
    timeout_watcher: asyncio.Task[None] | None = None
    with ExitStack() as owned:
        try:
            async with asyncio.timeout_at(deadline):
                sockets = server.sockets
                if len(sockets) != 1:
                    raise GuardFailure("executor fixture model listener mismatch")
                address: object = sockets[0].getsockname()
                match address:
                    case ("127.0.0.1", int(port)):
                        pass
                    case _:
                        raise GuardFailure("executor fixture model endpoint mismatch")
                token = secrets.token_urlsafe(32)
                recipe = prepare_native_inputs(
                    root,
                    workspace,
                    binary=Path(probe.CODEX),
                    port=port,
                    bearer=None,
                    executor_transport=ExecutorTransport.PRIVATE_STDIO,
                )
                environment = materialize_native_inputs(recipe.exec_server)
                backend = await spawn(
                    recipe.exec_server.argv, recipe.exec_server.cwd, environment
                )
                verify_executor_key_absent(backend)
                backend_reader, backend_writer = stdio(backend)
                action = GuardAction(
                    scenario.kind,
                    scenario.command,
                    probe.CALL_ID,
                    workspace,
                    probe.MARKER,
                    probe.PATCH_MARKER,
                )
                guard = ExecutorGuard(
                    backend_reader,
                    backend_writer,
                    token,
                    action,
                    probe.CONTROL / "executor-guard-permit.json",
                    deadline,
                    probe.MAX_FRAME_BYTES,
                    probe.MAX_MESSAGES,
                    scenario.hook_outcome,
                )
                endpoint = await guard.start()
                await verify_upgrade_refused(endpoint, None, 401)
                await verify_upgrade_refused(endpoint, "wrong", 401)
                await verify_non_upgrade_refused(endpoint, token)
                await verify_upgrade_refused(
                    endpoint, token, 400, subprotocol=f"{MALFORMED_HEADER_CANARY}-ws"
                )
                await verify_malformed_header_refused(endpoint)
                environment = materialize_native_inputs(recipe.app_server)
                environment["CODEX_API_KEY"] = probe.SYNTHETIC_BEARER
                app = await spawn(
                    recipe.app_server.argv, recipe.app_server.cwd, environment
                )
                if witness_peer:
                    app_pidfd = probe.open_child_pidfd(app.pid)
                    owned.callback(os.close, app_pidfd)
                    peer = ExecutorPeerWitness(app.pid, app_pidfd)
                    guard.bind_peer(peer)
                    await verify_upgrade_refused(endpoint, token, 403)
                    probe.require(
                        peer.unavailable == 0, "guard_peer_observation_unavailable"
                    )
                    probe.require(
                        peer.decoy_refused == 1
                        and peer.admitted == guard.native_frames == 0,
                        "guard_decoy_peer_not_distinguished",
                    )
                reader, writer = stdio(app)
                rpc = AppServerRpc(
                    reader,
                    writer,
                    clock=loop.time,
                    next_request_id=lambda: secrets.randbits(63),
                    max_frame_bytes=probe.MAX_FRAME_BYTES,
                    max_messages=probe.MAX_MESSAGES,
                )
                selected = SelectedEnvironment(
                    probe.EXECUTOR_ID,
                    endpoint,
                    Path("/bin/bash"),
                    workspace,
                    SELECTED_MODEL,
                    selected_managed_hook("selected-hook-timeout"),
                )
                thread = await prepare_selected_thread(
                    rpc,
                    probe.startup_identity(
                        scenario.case, Path(environment["CODEX_HOME"])
                    ),
                    selected,
                    token,
                    deadline=deadline,
                )
                guard.set_thread(thread)
                await verify_upgrade_refused(endpoint, token, 409)
                if scenario.timed_out:
                    listener = owned.enter_context(socket.socket(socket.AF_UNIX))
                    listener.setblocking(False)
                    listener.bind(str(probe.CONTROL / HOOK_TIMEOUT_SOCKET))
                    listener.listen(1)
                    timeout_watcher = asyncio.create_task(
                        witness_timeout(listener, scenario, (app, backend), guard)
                    )
                terminal = await observe_app_server_turn(
                    rpc,
                    thread_id=thread,
                    prompt="Run the selected fixture action.",
                    deadline=deadline,
                    observe=guard.observe,
                    on_started=guard.started,
                )
                guard.fence()
                if timeout_watcher is not None:
                    await timeout_watcher
                await guard.close_connection()
                await verify_upgrade_refused(endpoint, token, 409)
                await guard.close()
                probe.require(
                    terminal.status == "completed", "guard_turn_not_completed"
                )
                probe.require(
                    fixture.failure is None
                    and fixture.requests == fixture.connections == 2,
                    "guard_model_outcome_mismatch",
                )
                probe.require(
                    guard.failure is None
                    and guard.hook_matches(terminal.turn_id)
                    and (guard.attempted, guard.forwarded, guard.withheld)
                    == scenario.counts,
                    "guard_effect_outcome_mismatch",
                )
                probe.require(
                    guard.permit_matches()
                    == (scenario.mode in {"allow", "staged-timeout"}),
                    "guard_permit_outcome_mismatch",
                )
                hook_pid(scenario)
                receipt = probe.read_hook_receipt()
                probe.require(
                    receipt.get("session_id") == thread
                    and receipt.get("turn_id") == terminal.turn_id,
                    "guard_hook_receipt_identity_mismatch",
                )
                verify_marker(scenario, root, workspace)
                if peer is not None:
                    probe.require(
                        peer.admitted == peer.decoy_refused == 1
                        and peer.unavailable == 0,
                        "guard_selected_peer_not_distinguished",
                    )
                probe.diagnostic(
                    "INFO", "probe.selected_executor_guard_verified", scenario.case
                )
        finally:
            cleanup_deadline = loop.time() + probe.CLEANUP_SECONDS
            try:
                async with asyncio.timeout_at(cleanup_deadline):
                    if timeout_watcher is not None:
                        timeout_watcher.cancel()
                        await asyncio.gather(timeout_watcher, return_exceptions=True)
                    if guard is not None:
                        await guard.close()
            finally:
                await probe.close_selected_launch(
                    fixture, server, app, backend, cleanup_deadline
                )

        if peer is not None:
            probe.require(
                peer.sockets_closed() and loop.time() < cleanup_deadline,
                "guard_peer_sockets_open",
            )
            probe.diagnostic(
                "INFO", "probe.selected_executor_peer_verified", scenario.case
            )


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    witness_peer = sys.argv[1] == "peer-bash-allow"
    scenario = SCENARIOS.get("bash-allow" if witness_peer else sys.argv[1])
    if scenario is None:
        return 2
    try:
        with TemporaryDirectory(prefix="kora-codex-guard-", dir="/tmp") as scratch:
            asyncio.run(run_guard(Path(scratch), scenario, witness_peer=witness_peer))
    except probe.ProbeFailure as error:
        probe.diagnostic("ERROR", "probe.selected_executor_guard_failed", str(error))
        return 1
    except GuardFailure as error:
        probe.diagnostic("ERROR", "probe.selected_executor_guard_failed", str(error))
        return 1
    except (RpcError, TurnError, OSError, TimeoutError, ValueError) as error:
        probe.diagnostic(
            "ERROR", "probe.selected_executor_guard_failed", type(error).__name__
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
