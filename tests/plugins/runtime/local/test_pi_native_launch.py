import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import sys
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, cast

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    INSTANCE_ID,
    context_for,
    utc_now,
)
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import decode_strict_json
from kora.observability import Level, LoggingSettings
from kora.plugins.harness.pi.bridge_protocol import (
    MAX_FRAME_BYTES,
    BridgeChannel,
    BridgeRefused,
)
from kora.plugins.harness.pi.installation import (
    InstallationRefused,
    PiInstallation,
    verify_pi_installation,
)
from kora.plugins.harness.pi.shell_call import PiShellCall, ShellCallRefused
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.plugins.runtime.local.bound_checkout import BoundCheckout, BoundCheckoutError
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.scoped_shell import (
    BASH_PATH,
    OUTPUT_BYTES,
    ScopedShell,
    ScopedShellAdmission,
    ScopedShellError,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import (
    EnvironmentVariable,
    OperationId,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    new_operation_id,
)
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import ChannelError, ChannelFailure, wait_future
from kora.runner.child_wait import Readiness, native_direct_child_wait
from kora.runner.identity import NativeIdentityReader
from kora.runner.installed_executable import ExecutablePin, verify_installed_executable
from kora.runner.native_io import observed
from kora.runner.process_stdio import StdioStreams
from kora.runner.processes import GatedProcess, start_gated
from tests.plugins.harness.claude.scripted_model import end_stream, sse, tool_stream
from tests.plugins.harness.codex.selected_gated_stdio import native_environment
from tests.plugins.policy.policy_support import layers, policy, workspace
from tests.plugins.runtime.local.frames_support import StepClock
from tests.plugins.runtime.local.test_scoped_shell import (
    DESCENDANT,
    assert_gone,
    current_identity,
    gate_descriptors,
    python_command,
    subreaper,
)
from tests.support.local_peers import until

SECOND: Final[int] = 1_000_000_000
PROCESS_CLEANUP_NS: Final[int] = 6 * SECOND
MODEL: Final[str] = "claude-sonnet-4-5-20250929"
FIXTURE_KEY: Final[str] = "pi-sdk-scripted-credential-9f71"
PROMPT: Final[str] = "Produce the fixed witness value."
AMBIENT: Final[str] = "PI_DISCOVERY_CANARY_4ccbd"
MODEL_BYTES: Final[int] = 256 * 1024
BASH_CALL_ID: Final[str] = "toolu_scripted_0"
NONCE: Final[str] = "a09178ca09f84c2795d3c48a0679b741"


@dataclass(frozen=True)
class Request:
    line: str
    authorized: bool
    document: JsonObject


class ModelPeer:
    def __init__(
        self,
        *,
        hold: bool = False,
        redirect: str | None = None,
        padding_records: int = 0,
        response_gate: asyncio.Event | None = None,
        reported_model: str = MODEL,
        reported_stop: str = "end_turn",
        responses: tuple[bytes, ...] = (),
        key: str = FIXTURE_KEY,
    ) -> None:
        self.hold: bool = hold
        self.redirect: str | None = redirect
        self.padding_records: int = padding_records
        self.response_gate: asyncio.Event | None = response_gate
        self.reported_model: str = reported_model
        self.reported_stop: str = reported_stop
        self.responses = responses
        self.key = key
        self.connections = 0
        self.header_count = 0
        self.received: asyncio.Event = asyncio.Event()
        self.continuing: asyncio.Event = asyncio.Event()
        self.continuation_gate: asyncio.Event | None = None
        self.requests: list[Request] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.origin: str = ""

    def accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        if len(self.tasks) >= 2:
            writer.close()
            return
        self.tasks.add(asyncio.create_task(self.serve(reader, writer)))

    async def serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(10):
                headers = (
                    (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
                )
                fields = dict(
                    (key.lower(), value.strip())
                    for line in headers[1:-2]
                    for key, value in (line.split(":", 1),)
                )
                self.header_count += 1
                length = int(fields.get("content-length", "0"))
                if not 0 < length <= MODEL_BYTES:
                    return
                document = decode_strict_json(
                    await reader.readexactly(length), MODEL_BYTES, 16
                )
                if not isinstance(document, dict):
                    return
                response_index = len(self.requests)
                self.requests.append(
                    Request(headers[0], fields.get("x-api-key") == self.key, document)
                )
                self.received.set()
                if response_index == 1:
                    self.continuing.set()
                    if self.continuation_gate is not None:
                        await self.continuation_gate.wait()
                if self.response_gate is not None:
                    await self.response_gate.wait()
                if self.redirect is not None:
                    headers_out = (
                        "HTTP/1.1 307 Temporary Redirect\r\n"
                        f"Location: {self.redirect}/v1/messages?beta=true\r\n"
                        "Content-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    writer.write(headers_out.encode())
                elif self.hold:
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                    )
                    await writer.drain()
                    await reader.read(1)
                else:
                    comment = b": " + b"x" * 2996 + b"\n\n"
                    body = comment * self.padding_records + end_stream(
                        self.reported_model
                    ).replace(b'"end_turn"', f'"{self.reported_stop}"'.encode())
                    if self.responses:
                        if response_index >= len(self.responses):
                            return
                        body = self.responses[response_index]
                    headers_out = (
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                    )
                    writer.write(headers_out.encode() + body)
                await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                if (
                    not self.hold
                    and self.response_gate is None
                    and not self.padding_records
                    and not self.responses
                ):
                    raise


@asynccontextmanager
async def model_peer(
    *,
    hold: bool = False,
    redirect: str | None = None,
    padding_records: int = 0,
    response_gate: asyncio.Event | None = None,
    reported_model: str = MODEL,
    reported_stop: str = "end_turn",
    responses: tuple[bytes, ...] = (),
    key: str = FIXTURE_KEY,
) -> AsyncGenerator[ModelPeer]:
    peer = ModelPeer(
        hold=hold,
        redirect=redirect,
        padding_records=padding_records,
        response_gate=response_gate,
        reported_model=reported_model,
        reported_stop=reported_stop,
        responses=responses,
        key=key,
    )
    server = await asyncio.start_server(peer.accept, "127.0.0.1", 0, limit=16_384)
    assert server.sockets
    _, port = cast(tuple[str, int], server.sockets[0].getsockname())
    peer.origin = f"http://127.0.0.1:{port}"
    try:
        async with server:
            yield peer
    finally:
        async with asyncio.timeout(5):
            await asyncio.gather(*peer.tasks)


@pytest.fixture(scope="module")
def stage_path() -> Path:
    return Path(os.environ["KORA_PI_SDK_STAGE"])


@pytest.fixture
def kora_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(level=Level.DEBUG), INSTANCE_ID, utc_now
    ) as capture:
        yield capture


@pytest.fixture(scope="module")
def installation(stage_path: Path) -> PiInstallation:
    clock = SystemClock()
    return verify_pi_installation(stage_path, clock.monotonic_ns() + 15 * SECOND, clock)


@pytest.fixture
def copied_stage(stage_path: Path, tmp_path: Path) -> Generator[Path]:
    with TemporaryDirectory(dir=tmp_path.resolve()) as directory:
        yield shutil.copytree(stage_path, Path(directory) / "stage")


def seed_discovery(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "settings.json").write_text('{"defaultModel":"' + AMBIENT + '"}')
    (root / "AGENTS.md").write_text(AMBIENT)
    extensions = root / "extensions"
    extensions.mkdir()
    (extensions / "canary.ts").write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "export default function check() {\n"
        "  writeFileSync('pi-extension-loaded', '1');\n"
        "}\n"
    )
    (root / "commands").mkdir()


def fixture_paths(root: Path) -> dict[Path, int]:
    return {path: stat.S_IFMT(path.lstat().st_mode) for path in root.rglob("*")}


class FixtureFiles:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths = fixture_paths(root)
        self.files = {
            path: path.read_bytes()
            for path, kind in self.paths.items()
            if kind == stat.S_IFREG
        }

    def assert_unchanged(self) -> None:
        assert fixture_paths(self.root) == self.paths
        for path, content in self.files.items():
            assert path.stat().st_size == len(content)
            assert path.read_bytes() == content


class PiGate:
    def __init__(
        self,
        installation: PiInstallation,
        checkout: BoundCheckout,
        home: Path,
        scan_clock: Clock | None,
        reaper: AdoptedReaper | None = None,
    ) -> None:
        self.installation: PiInstallation = installation
        self.checkout: BoundCheckout = checkout
        self.clock: SystemClock = SystemClock()
        self._scan_clock: Clock = self.clock if scan_clock is None else scan_clock
        self._installation_scan: asyncio.Task[None] | None = None
        self._release_task: asyncio.Task[int] | None = None
        self.identities = NativeIdentityReader(clock=self.clock)
        self._child_probe, self._watch_child = native_direct_child_wait()
        self.cutoff_ns: int = self.clock.monotonic_ns() + 10 * SECOND
        self.nonce: str = new_operation_id()
        self.stopped: bool = False
        self.checkout.verify(self.checkout.path, self.cutoff_ns, self.clock)
        installation.recheck(self.cutoff_ns, self.clock)
        context = (checkout.path, home)
        if any(
            path.is_relative_to(installation.root)
            or installation.root.is_relative_to(path)
            for path in context
        ):
            raise InstallationRefused()
        argv = (
            str(installation.node),
            "--no-global-search-paths",
            str(installation.bridge),
        )
        self.recipe: ProcessLaunch = ProcessLaunch(
            executable=argv[0],
            argv=argv,
            environment=(
                EnvironmentVariable(name="HOME", value=str(home)),
                EnvironmentVariable(name="TMPDIR", value=str(home)),
                EnvironmentVariable(name="PI_OFFLINE", value="1"),
            ),
            working_directory_resource_id="1" * 32,
            stdin=None,
            stdout=None,
            stderr=ProcessOutput.CAPTURE,
            domain=ProcessDomain.DETACHED,
        )
        self.owner: GatedProcess = start_gated(
            self.recipe,
            self.nonce,
            checkout.anchor.descriptor,
            self.cutoff_ns,
            self.clock,
            self.identities,
            stdio_limit=MAX_FRAME_BYTES - 1,
            wait_owner=reaper,
        )
        self.shell: PiShellCall | None = None
        self.channel: BridgeChannel | None = None
        self.streams: StdioStreams
        self.pid: int

    async def start(
        self,
        origin: str,
        model: str = MODEL,
        barrier: tuple[asyncio.Event, asyncio.Event] | None = None,
    ) -> None:
        registration = await self.owner.prepare()
        self.pid = registration.process.pid
        if barrier is not None:
            appeared, approval = barrier
            appeared.set()
            await approval.wait()
        await self.recheck_installation()
        self.checkout.verify(self.checkout.path, self.cutoff_ns, self.clock)
        await self.owner.acknowledge()
        self.streams = self.owner.borrow_stdio()
        self.channel = BridgeChannel(
            self.streams,
            self.nonce,
            self.cutoff_ns,
            self.clock,
            self.require_binding,
            self.recheck_installation,
            self.shell,
            self.release_revoked if self.shell is not None else None,
        )
        await self.channel.initialize(origin, model)

    def require_binding(self, allow_exit: bool = False) -> None:
        if self.stopped:
            raise BridgeRefused("stopped")
        self.checkout.verify(self.checkout.path, self.cutoff_ns, self.clock)
        self.owner.require_acquired()
        registration = self.owner.registration
        if registration is None:
            raise BridgeRefused("stopped")
        process = self.identities.read(self.pid).process
        if (
            process is not None
            and process.identity == registration.process
            and process.process_group_id == registration.process_group_id
            and process.session_id == registration.session_id
            and process.parent_pid == os.getpid()
            and (allow_exit or not process.dead)
        ):
            return
        if (
            process is None
            and allow_exit
            and self._child_probe is not None
            and self._child_probe.read(registration) is Readiness.READY
        ):
            return
        raise BridgeRefused("stopped")

    async def wait_for_terminal_exit(self) -> None:
        probe = self._child_probe
        if probe is None:
            await self.owner.wait_exit(self.cutoff_ns)
            return

        watch_child = self._watch_child
        assert watch_child is not None
        self.require_binding()
        registration = self.owner.registration
        assert registration is not None
        assert probe.read(registration) is Readiness.NOT_READY
        await self.owner.wait_exit(self.cutoff_ns)
        while self.clock.monotonic_ns() < self.cutoff_ns:
            watch = watch_child(registration.process.pid)
            try:
                assert self.owner.registration == registration
                readiness = probe.read(registration)
                assert readiness is not Readiness.MISMATCH
                if readiness is Readiness.READY:
                    break
                await wait_future(watch.changed, self.cutoff_ns, self.clock)
            finally:
                watch.close()
        else:
            raise ChannelError(ChannelFailure.DEADLINE)
        self.require_binding(allow_exit=True)

    async def recheck_installation(self) -> None:
        if self.stopped or self._release_task is not None:
            raise BridgeRefused("stopped")
        if self._installation_scan is not None:
            raise BridgeRefused("protocol")
        scan = asyncio.create_task(
            asyncio.to_thread(
                self.installation.recheck, self.cutoff_ns, self._scan_clock
            )
        )
        self._installation_scan = scan
        try:
            await asyncio.shield(scan)
        except asyncio.CancelledError:
            self._begin_release(natural=False)
            raise
        finally:
            if scan.done():
                self._installation_scan = None

    async def key(self, capture: Capture, key: str = FIXTURE_KEY) -> None:
        self.require_binding()
        await self.recheck_installation()
        self.require_binding()
        assert self.channel is not None
        await self.channel.credential(capture.scope.admit(key))

    async def prompt(self, text: str) -> str:
        self.require_binding()
        await self.recheck_installation()
        self.require_binding()
        assert self.channel is not None
        return await self.channel.prompt(text)

    def stop(self) -> None:
        self.stopped = True
        if self.channel is not None:
            self.channel.revoke()
        self.owner.close_gate()

    def _begin_release(self, *, natural: bool) -> asyncio.Task[int]:
        if self._release_task is None:
            natural = natural and self._installation_scan is None
            if not natural:
                self.stopped = True
            self._release_task = asyncio.create_task(
                self._settle_release(natural=natural)
            )
        return self._release_task

    def release_revoked(self) -> None:
        self._begin_release(natural=False)

    async def release(self, *, natural: bool) -> int:
        release = self._begin_release(natural=natural)
        cancelled = await observed(release)
        if self._release_task is release and not self.owner.closed:
            self._release_task = None
        if cancelled:
            raise asyncio.CancelledError
        return release.result()

    async def _settle_release(self, *, natural: bool) -> int:
        cutoff_ns = self.clock.monotonic_ns() + PROCESS_CLEANUP_NS
        try:
            if natural:
                self.owner.close_gate()
                await self.owner.settle(cutoff_ns)
            else:
                self.stop()
                self.owner.signal(signal.SIGTERM, cutoff_ns)
                term_ns = min(cutoff_ns, self.clock.monotonic_ns() + 2 * SECOND)
                try:
                    await self.owner.settle(term_ns)
                except ChannelError as error:
                    if error.reason is not ChannelFailure.DEADLINE:
                        raise
                    self.owner.signal(signal.SIGKILL, cutoff_ns)
                    await self.owner.settle(cutoff_ns)
            await self.owner.drain(cutoff_ns)
        finally:
            if self._installation_scan is not None:
                await observed(self._installation_scan)
                self._installation_scan = None
        return self.owner.release(self.clock.monotonic_ns() + PROCESS_CLEANUP_NS)

    def assert_reaped(self) -> None:
        assert self.owner.closed
        assert self.channel is None or self.streams[1].is_closing()
        with pytest.raises(ChildProcessError):
            os.waitpid(self.pid, os.WNOHANG)


@asynccontextmanager
async def witness(
    installation: PiInstallation,
    directory: Path,
    *,
    scan_clock: Clock | None = None,
    reaper: AdoptedReaper | None = None,
) -> AsyncGenerator[PiGate]:
    root = directory.resolve()
    checkout_path = root / "checkout"
    seed_discovery(checkout_path)
    seed_discovery(checkout_path / ".pi")
    home = root / "role"
    home.mkdir(mode=0o700)
    ambient_agent = home / ".pi" / "agent"
    seed_discovery(ambient_agent)
    (ambient_agent / "auth.json").write_text(
        '{"anthropic":{"type":"api_key","key":"' + AMBIENT + '"}}'
    )
    checkout = BoundCheckout.capture(checkout_path)
    try:
        owner = PiGate(installation, checkout, home, scan_clock, reaper)
        try:
            yield owner
        finally:
            try:
                if not owner.owner.closed:
                    await owner.release(natural=False)
            finally:
                if owner.owner.closed:
                    shutil.rmtree(home)
    finally:
        checkout.close()


@pytest.mark.asyncio
async def test_pi_sdk_uses_closed_session_and_reaps(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        original_entries = frozenset(tmp_path.rglob("*"))
        await gate.start(peer.origin)
        assert peer.requests == []
        await gate.key(kora_capture)
        assert peer.requests == []
        reader = NativeIdentityReader(clock=gate.clock)
        arguments = reader.arguments(gate.pid)
        environment = native_environment(gate.pid) if sys.platform == "linux" else None
        transport = gate.streams[1].transport
        assert isinstance(transport, asyncio.Transport)

        async with asyncio.TaskGroup() as tasks:
            transport.pause_reading()
            try:
                pending = tasks.create_task(gate.prompt(PROMPT))
                await gate.wait_for_terminal_exit()
            finally:
                transport.resume_reading()
        text = pending.result()
        result = await gate.release(natural=True)

        assert (text, result) == ("done", 0)
        gate.assert_reaped()
        assert len(peer.requests) == 1
        (request,) = peer.requests
        assert request.line == "POST /v1/messages?beta=true HTTP/1.1"
        assert request.authorized
        assert request.document["model"] == MODEL
        assert request.document["stream"] is True
        assert request.document.get("tools", []) == []
        assert request.document["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": PROMPT}]}
        ]
        assert AMBIENT not in repr(request.document)
        assert FIXTURE_KEY not in repr(request.document)
        assert FIXTURE_KEY not in repr(gate.recipe)
        assert FIXTURE_KEY not in repr(kora_capture.finish())
        assert arguments == tuple(os.fsencode(value) for value in gate.recipe.argv)
        if sys.platform == "linux":
            assert environment == frozenset(
                os.fsencode(f"{item.name}={item.value}")
                for item in gate.recipe.environment
            ).union({b""})
        assert frozenset(tmp_path.rglob("*")) == original_entries
        assert all(
            FIXTURE_KEY.encode() not in file.read_bytes()
            for file in tmp_path.rglob("*")
            if file.is_file()
        )


@pytest.mark.asyncio
async def test_pi_sdk_refuses_replaced_checkout_before_key(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        await gate.start(peer.origin)
        original = gate.checkout.path
        original.rename(original.with_name("previous"))
        original.mkdir(mode=0o700)

        with pytest.raises(BoundCheckoutError):
            await gate.key(kora_capture)
        await gate.release(natural=False)

        gate.assert_reaped()
        assert peer.requests == []


@pytest.mark.asyncio
async def test_pi_sdk_stop_interrupts_blocked_provider_body(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    async with model_peer(hold=True) as peer, witness(installation, tmp_path) as gate:
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        prompt = asyncio.create_task(gate.prompt(PROMPT))
        async with asyncio.timeout(5):
            await peer.received.wait()

        await gate.release(natural=False)
        with pytest.raises(BridgeRefused):
            await prompt

        gate.assert_reaped()
        assert len(peer.requests) == 1


@pytest.mark.asyncio
async def test_pi_sdk_does_not_forward_redirect(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    async with model_peer() as destination:
        async with (
            model_peer(redirect=destination.origin) as selected,
            witness(installation, tmp_path) as gate,
        ):
            await gate.start(selected.origin)
            await gate.key(kora_capture)

            with pytest.raises(BridgeRefused) as failure:
                await gate.prompt(PROMPT)
            result = await gate.release(natural=True)

            assert failure.value.code == "prompt"
            assert result == 1
            gate.assert_reaped()
            assert len(selected.requests) == 1
            assert destination.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("over_limit", [False, True], ids=["under-limit", "over-limit"])
async def test_pi_sdk_caps_valid_cumulative_provider_body(
    installation: PiInstallation,
    tmp_path: Path,
    kora_capture: Capture,
    over_limit: bool,
) -> None:
    async with (
        model_peer(padding_records=88 if over_limit else 86) as selected,
        witness(installation, tmp_path) as gate,
    ):
        await gate.start(selected.origin)
        await gate.key(kora_capture)

        if over_limit:
            with pytest.raises(BridgeRefused) as failure:
                await gate.prompt(PROMPT)
            assert failure.value.code == "prompt"
        else:
            assert await gate.prompt(PROMPT) == "done"
        result = await gate.release(natural=True)

    assert result == int(over_limit)
    assert len(selected.requests) == 1
    gate.assert_reaped()


@pytest.mark.parametrize("replacement", ["changed", "symlink", "late"])
def test_pi_installation_refuses_changed_entry(
    stage_path: Path, copied_stage: Path, replacement: str
) -> None:
    entry = copied_stage / "installed/bridge/dist/bridge.js"
    clock = SystemClock()
    cutoff_ns = clock.monotonic_ns() + 15 * SECOND
    admission = (
        verify_pi_installation(copied_stage, cutoff_ns, clock)
        if replacement == "late"
        else None
    )
    if replacement in ("changed", "late"):
        entry.write_bytes(b"refused")
    else:
        entry.unlink()
        entry.symlink_to(stage_path / "installed/bridge/dist/bridge.js")
    check = (
        partial(verify_pi_installation, copied_stage, cutoff_ns, clock)
        if admission is None
        else partial(admission.recheck, cutoff_ns, clock)
    )

    with pytest.raises(InstallationRefused, match="pi_installation_refused"):
        check()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        b"{\n",
        b"x" * MAX_FRAME_BYTES + b"\n",
        b'{"kind":"stop","nonce":"00000000000000000000000000000000"}\n',
    ],
    ids=["malformed", "oversized", "uncorrelated"],
)
async def test_pi_sdk_refuses_bad_control_before_key(
    installation: PiInstallation, tmp_path: Path, record: bytes
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        await gate.start(peer.origin)
        reader, writer = gate.streams

        writer.write(record)
        async with asyncio.timeout(5):
            await writer.drain()
            failure = decode_strict_json(
                await reader.readuntil(b"\n"), MAX_FRAME_BYTES, 1
            )
        result = await gate.release(natural=True)

        assert isinstance(failure, dict)
        challenge = failure.pop("challenge")
        assert isinstance(challenge, str)
        assert len(challenge) == 32
        assert failure == {"kind": "failure", "nonce": gate.nonce, "code": "protocol"}
        assert result == 1
        gate.assert_reaped()
        assert peer.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("activation", ["eof", "malformed", "stale"])
async def test_pi_sdk_refuses_unactivated_prompt(
    installation: PiInstallation,
    tmp_path: Path,
    kora_capture: Capture,
    activation: str,
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        await gate.start(peer.origin)
        reader, writer = gate.streams
        challenge = new_operation_id()
        activate = (
            f'{{"kind":"activate","nonce":"{gate.nonce}","challenge":"{challenge}"}}\n'
        ).encode()
        key = kora_capture.scope.admit(FIXTURE_KEY).reveal()
        credential = (
            f'{{"kind":"credential","nonce":"{gate.nonce}","key":"{key}"}}\n'
        ).encode()
        writer.write(credential + activate)
        async with asyncio.timeout(5):
            ready = decode_strict_json(
                await reader.readuntil(b"\n"), MAX_FRAME_BYTES, 1
            )
        assert ready == {
            "kind": "credential_ready",
            "nonce": gate.nonce,
            "challenge": challenge,
        }
        prompt = (
            f'{{"kind":"prompt","nonce":"{gate.nonce}","text":"{PROMPT}"}}\n'.encode()
        )
        proof = {"eof": b"", "malformed": b'{"kind":"activate"}\n', "stale": activate}

        writer.write(prompt + proof[activation])
        writer.write_eof()
        async with asyncio.timeout(5):
            failure = decode_strict_json(
                await reader.readuntil(b"\n"), MAX_FRAME_BYTES, 1
            )
        result = await gate.release(natural=True)

    assert isinstance(failure, dict)
    assert failure.get("code") == ("stopped" if activation == "eof" else "protocol")
    assert failure.get("kind") == "failure"
    assert peer.requests == []
    assert result == 1
    gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_sdk_refuses_failed_startup(
    installation: PiInstallation, tmp_path: Path
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        files = FixtureFiles(tmp_path)

        with pytest.raises(BridgeRefused) as failure:
            await gate.start(peer.origin, "fixture-model-not-in-catalog")
        result = await gate.release(natural=True)
        files.assert_unchanged()

    assert failure.value.code == "startup"
    assert result == 1
    gate.assert_reaped()
    assert peer.requests == []
    assert not (tmp_path / "role").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_key", [False, True], ids=["before-key", "before-prompt"])
async def test_pi_sdk_stop_fences_admission(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, with_key: bool
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        await gate.start(peer.origin)
        if with_key:
            await gate.key(kora_capture)
        action = (
            partial(gate.prompt, PROMPT)
            if with_key
            else partial(gate.key, kora_capture)
        )

        gate.stop()
        assert not gate.owner.closed
        with pytest.raises(BridgeRefused, match="pi_bridge_refused") as failure:
            await action()
        await gate.release(natural=False)

    assert failure.value.code == "stopped"
    gate.assert_reaped()
    assert peer.requests == []


@pytest.mark.asyncio
async def test_pi_sdk_lost_terminal_keeps_original_process_owner(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    send_response = asyncio.Event()
    async with (
        model_peer(response_gate=send_response) as peer,
        witness(installation, tmp_path) as gate,
    ):
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        prompt = asyncio.create_task(gate.prompt(PROMPT))
        async with asyncio.timeout(5):
            await peer.received.wait()

        gate.stop()
        send_response.set()
        with pytest.raises(BridgeRefused) as failure:
            await prompt
        await gate.release(natural=False)

    assert failure.value.code == "closed"
    gate.assert_reaped()
    assert len(peer.requests) == 1


@pytest.mark.asyncio
async def test_pi_sdk_refuses_terminal_after_checkout_replacement(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    send_response = asyncio.Event()
    async with (
        model_peer(response_gate=send_response) as peer,
        witness(installation, tmp_path) as gate,
    ):
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        pending = asyncio.create_task(gate.prompt(PROMPT))
        async with asyncio.timeout(5):
            await peer.received.wait()

        original = gate.checkout.path
        original.rename(original.with_name("previous-checkout"))
        original.mkdir()
        send_response.set()
        with pytest.raises(BoundCheckoutError):
            await pending
        result = await gate.release(natural=True)

    assert len(peer.requests) == 1
    assert peer.requests[0].authorized
    assert result == 0
    gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_sdk_abrupt_exit_leaves_no_fixture_files(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    async with model_peer(hold=True) as peer, witness(installation, tmp_path) as gate:
        files = FixtureFiles(tmp_path)
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        prompt = asyncio.create_task(gate.prompt(PROMPT))
        async with asyncio.timeout(5):
            await peer.received.wait()
        assert len(peer.requests) == 1
        assert peer.requests[0].authorized
        gate.require_binding()

        os.kill(gate.pid, signal.SIGKILL)
        with pytest.raises(BridgeRefused) as failure:
            await prompt
        result = await gate.release(natural=True)

        assert failure.value.code == "closed"
        assert result == -signal.SIGKILL
        files.assert_unchanged()
        assert FIXTURE_KEY not in repr(kora_capture.finish())
        gate.assert_reaped()
        assert len(peer.requests) == 1


@asynccontextmanager
async def prepared_channel(
    capture: Capture,
    clock: Clock,
    require_binding: Callable[[bool], None] | None = None,
    recheck_installation: Callable[[], Awaitable[None]] | None = None,
    shell: PiShellCall | None = None,
) -> AsyncGenerator[tuple[BridgeChannel, asyncio.StreamReader, StdioStreams, str]]:
    socket_end, held = socket.socketpair()
    with socket_end, held:
        socket_end.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        streams = await asyncio.open_connection(sock=socket_end)
        peer = await asyncio.open_connection(sock=held)

        def socket_live(_allow_exit: bool = False) -> None:
            if socket_end.fileno() < 0:
                raise BridgeRefused("closed")

        async def socket_recheck() -> None:
            socket_live()

        channel = BridgeChannel(
            streams,
            NONCE,
            clock.monotonic_ns() + 5 * SECOND,
            clock,
            require_binding or socket_live,
            recheck_installation or socket_recheck,
            shell,
        )
        reader, writer = streams
        peer_reader, peer_writer = peer
        try:
            async with asyncio.timeout(5):
                init = asyncio.create_task(
                    channel.initialize("http://127.0.0.1:1", MODEL)
                )
                init_challenge = await control_challenge(peer_reader, "init")
                peer_writer.write(bridge_reply("ready", init_challenge))
                await peer_writer.drain()
                await init
                key = asyncio.create_task(
                    channel.credential(capture.scope.admit(FIXTURE_KEY))
                )
                key_challenge = await control_challenge(peer_reader, "credential")
                assert key_challenge != init_challenge
                peer_writer.write(bridge_reply("credential_ready", key_challenge))
                await peer_writer.drain()
                await key
            yield channel, reader, peer, key_challenge
        finally:
            channel.revoke()
            peer_writer.close()
            await asyncio.gather(writer.wait_closed(), peer_writer.wait_closed())


async def control_challenge(reader: asyncio.StreamReader, kind: str) -> str:
    frame = decode_strict_json(await reader.readline(), MAX_FRAME_BYTES, 1)
    assert isinstance(frame, dict)
    assert frame.get("kind") == kind
    assert frame.get("nonce") == NONCE
    assert "challenge" not in frame
    activation = decode_strict_json(await reader.readline(), MAX_FRAME_BYTES, 1)
    assert isinstance(activation, dict)
    challenge = activation.get("challenge")
    assert isinstance(challenge, str)
    assert len(challenge) == 32
    assert activation == {"kind": "activate", "nonce": NONCE, "challenge": challenge}
    return challenge


def bridge_reply(kind: str, challenge: str) -> bytes:
    text = ',"text":"done"' if kind == "terminal" else ""
    return (
        f'{{"kind":"{kind}","nonce":"{NONCE}","challenge":"{challenge}"{text}}}\n'
    ).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("expire", [False, True], ids=["stop", "cutoff"])
async def test_pi_bridge_closes_with_pending_control_write(
    kora_capture: Capture, expire: bool
) -> None:
    clock = StepClock()
    async with prepared_channel(kora_capture, clock) as (channel, _, peer, _):
        peer_reader, peer_writer = peer
        transport = peer_writer.transport
        assert isinstance(transport, asyncio.Transport)
        transport.pause_reading()
        pending = asyncio.create_task(channel.prompt("x" * 32_768))
        async with asyncio.timeout(5):
            await clock.waiting.wait()

        if expire:
            clock.advance(5 * SECOND)
        else:
            channel.revoke()
        with pytest.raises(BridgeRefused) as failure:
            await pending
        transport.resume_reading()
        async with asyncio.timeout(5):
            partial_frame = await peer_reader.read()

    assert failure.value.code == ("deadline" if expire else "closed")
    assert partial_frame.startswith(b'{"kind":"prompt"')
    assert b"\n" not in partial_frame


@pytest.mark.asyncio
async def test_pi_bridge_refuses_resumed_write_after_checkout_replacement(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    clock = StepClock()
    async with model_peer() as model, witness(installation, tmp_path) as gate:
        await gate.start(model.origin)
        async with prepared_channel(
            kora_capture, clock, gate.require_binding, gate.recheck_installation
        ) as (channel, _, peer, _):
            transport = peer[1].transport
            assert isinstance(transport, asyncio.Transport)
            transport.pause_reading()
            pending = asyncio.create_task(channel.prompt("x" * 32_768))
            async with asyncio.timeout(5):
                await clock.waiting.wait()

            original = gate.checkout.path
            original.rename(original.with_name("previous-checkout"))
            original.mkdir()
            transport.resume_reading()
            with pytest.raises(BoundCheckoutError):
                await pending
            async with asyncio.timeout(5):
                partial_frame = await peer[0].read()
        await gate.release(natural=False)

    assert partial_frame.startswith(b'{"kind":"prompt"')
    assert b"\n" not in partial_frame
    assert model.requests == []
    gate.assert_reaped()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["closed", "deadline"])
@pytest.mark.parametrize(
    "buffered", ["terminal", "malformed", "oversized", "incomplete"]
)
async def test_pi_bridge_preserves_stop_cause_for_buffered_reply(
    kora_capture: Capture,
    stop: str,
    buffered: str,
) -> None:
    clock = StepClock()
    async with prepared_channel(kora_capture, clock) as (
        channel,
        reader,
        peer,
        _,
    ):
        pending = asyncio.create_task(channel.prompt(PROMPT))
        async with asyncio.timeout(5):
            challenge = await control_challenge(peer[0], "prompt")
        with pytest.raises(BridgeRefused) as duplicate:
            await channel.prompt(PROMPT)

        payload = {
            "terminal": bridge_reply("terminal", challenge),
            "malformed": b"{\n",
            "oversized": b"x" * (MAX_FRAME_BYTES + 1),
            "incomplete": b"{",
        }[buffered]
        reader.feed_data(payload)
        if buffered == "incomplete":
            reader.feed_eof()
        if stop == "closed":
            channel.revoke()
        else:
            clock.advance(5 * SECOND)
        with pytest.raises(BridgeRefused) as stopped:
            await pending

    assert duplicate.value.code == "protocol"
    assert stopped.value.code == stop


class ReadTimerLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self.offset: float = 0.0

    def time(self) -> float:
        return super().time() + self.offset


@pytest.mark.parametrize("revoked", [False, True], ids=["deadline", "revoked"])
def test_pi_bridge_read_timer_preserves_latched_stop(
    kora_capture: Capture,
    revoked: bool,
) -> None:
    loop = ReadTimerLoop()

    async def exercise() -> str:
        async with prepared_channel(kora_capture, StepClock()) as (channel, _, peer, _):

            def expire_read_timer() -> None:
                if revoked:
                    channel.revoke()
                loop.offset = 6.0

            pending = loop.create_task(channel.prompt(PROMPT))
            loop.call_soon(expire_read_timer)
            with pytest.raises(BridgeRefused) as stopped:
                await pending
            async with asyncio.timeout(5):
                await control_challenge(peer[0], "prompt")
            return stopped.value.code

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        cause = runner.run(exercise())

    assert cause == ("closed" if revoked else "deadline")


@pytest.mark.asyncio
async def test_pi_bridge_refuses_unsolicited_terminal(kora_capture: Capture) -> None:
    async with prepared_channel(kora_capture, StepClock()) as (channel, _, peer, key):
        peer[1].write(bridge_reply("terminal", key))
        await peer[1].drain()

        with pytest.raises(BridgeRefused) as failure:
            await channel.prompt(PROMPT)

    assert failure.value.code == "protocol"


@pytest.mark.asyncio
async def test_pi_bridge_holds_activation_until_prompt_data_completes(
    kora_capture: Capture,
) -> None:
    clock = StepClock()
    refresh_entered, refresh_release = asyncio.Event(), asyncio.Event()

    async def hold_refresh() -> None:
        refresh_entered.set()
        await refresh_release.wait()

    async with prepared_channel(
        kora_capture, clock, recheck_installation=hold_refresh
    ) as (
        channel,
        _,
        peer,
        prior_challenge,
    ):
        peer_reader, peer_writer = peer
        transport = peer_writer.transport
        assert isinstance(transport, asyncio.Transport)
        transport.pause_reading()
        pending = asyncio.create_task(channel.prompt("x" * 32_768))
        try:
            async with asyncio.timeout(5):
                await clock.waiting.wait()
                transport.resume_reading()
                partial_frame = await peer_reader.readexactly(1024)
                await refresh_entered.wait()

                peer_writer.write(bridge_reply("terminal", prior_challenge))
                await peer_writer.drain()
        finally:
            refresh_release.set()
        with pytest.raises(BridgeRefused) as failure:
            await pending

    assert partial_frame.startswith(b'{"kind":"prompt"')
    assert b"challenge" not in partial_frame
    assert b"\n" not in partial_frame
    assert failure.value.code == "protocol"


async def assert_unadmitted_terminal_refused(gate: PiGate) -> None:
    async def assert_prompt_refused() -> None:
        with pytest.raises(BridgeRefused) as failure:
            await gate.prompt(PROMPT)
        assert failure.value.code == "prompt"

    if sys.platform != "darwin":
        await assert_prompt_refused()
        return

    transport = gate.streams[1].transport
    assert isinstance(transport, asyncio.Transport)
    async with asyncio.TaskGroup() as tasks:
        transport.pause_reading()
        try:
            tasks.create_task(assert_prompt_refused())
            await gate.wait_for_terminal_exit()
        finally:
            transport.resume_reading()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_model", "reported_stop"),
    [("different-physical-model", "end_turn"), ("", "end_turn"), (MODEL, "pause_turn")],
    ids=["changed-model", "empty-model", "incomplete-turn"],
)
async def test_pi_sdk_refuses_unadmitted_terminal(
    installation: PiInstallation,
    tmp_path: Path,
    kora_capture: Capture,
    reported_model: str,
    reported_stop: str,
) -> None:
    async with (
        model_peer(reported_model=reported_model, reported_stop=reported_stop) as peer,
        witness(installation, tmp_path) as gate,
    ):
        await gate.start(peer.origin)
        await gate.key(kora_capture)

        await assert_unadmitted_terminal_refused(gate)
        result = await gate.release(natural=True)

    assert result == 1
    assert len(peer.requests) == 1
    gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_sdk_stops_during_partial_credential_frame(
    installation: PiInstallation, tmp_path: Path
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        await gate.start(peer.origin)
        reader, writer = gate.streams
        prefix = f'{{"kind":"credential","nonce":"{gate.nonce}","key":"synthetic'

        writer.write(prefix.encode())
        await writer.drain()
        await gate.release(natural=False)

        assert await reader.read() == b""
        assert peer.requests == []
        gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_sdk_cancellation_during_startup_reaps(
    installation: PiInstallation, tmp_path: Path
) -> None:
    started, activate = asyncio.Event(), asyncio.Event()
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        pending = asyncio.create_task(
            gate.start(peer.origin, barrier=(started, activate))
        )
        async with asyncio.timeout(5):
            await started.wait()

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await gate.release(natural=False)

    assert peer.requests == []
    gate.assert_reaped()


class HeldInstallationClock(SystemClock):
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.resume = threading.Event()
        self._samples_until_walk = 0

    def hold_next_scan(self) -> None:
        self._samples_until_walk = 2

    def monotonic_ns(self) -> int:
        if self._samples_until_walk:
            self._samples_until_walk -= 1
            if self._samples_until_walk == 0:
                self._loop.call_soon_threadsafe(self.entered.set)
                assert self.resume.wait(12)
        return super().monotonic_ns()


@pytest.mark.asyncio
async def test_pi_sdk_cancellation_joins_entered_installation_scan(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    scan_clock = HeldInstallationClock()
    role = tmp_path / "role"
    acquired: asyncio.Future[PiGate] = asyncio.get_running_loop().create_future()
    async with model_peer() as peer:

        async def run_witness() -> None:
            async with witness(installation, tmp_path, scan_clock=scan_clock) as gate:
                await gate.start(peer.origin)
                acquired.set_result(gate)
                scan_clock.hold_next_scan()
                await gate.key(kora_capture)

        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(run_witness())
            try:
                async with asyncio.timeout(5):
                    gate = await acquired
                    await scan_clock.entered.wait()

                pending.cancel()
                await gate.owner.wait_exit(gate.clock.monotonic_ns() + 4 * SECOND)
                await gate.clock.sleep_until(
                    gate.clock.monotonic_ns() + PROCESS_CLEANUP_NS
                )
                assert gate.stopped
                assert not gate.owner.closed
                assert role.is_dir()
                assert not pending.done()
            finally:
                scan_clock.resume.set()
            with pytest.raises(asyncio.CancelledError):
                await pending

    assert kora_capture.scope.registered_count() == 0
    assert peer.requests == []
    assert not role.exists()
    gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_sdk_rechecks_installation_before_activation(
    copied_stage: Path, tmp_path: Path, kora_capture: Capture
) -> None:
    root = tmp_path.resolve()
    prepared, activate = asyncio.Event(), asyncio.Event()
    clock = SystemClock()
    saved = verify_pi_installation(
        copied_stage, clock.monotonic_ns() + 15 * SECOND, clock
    )
    async with model_peer() as peer, witness(saved, root / "attempt") as gate:
        pending = asyncio.create_task(
            gate.start(peer.origin, barrier=(prepared, activate))
        )
        async with asyncio.timeout(5):
            await prepared.wait()

        saved.bridge.write_bytes(b"changed")
        activate.set()
        with pytest.raises(InstallationRefused):
            await pending
        await gate.release(natural=False)

    assert kora_capture.scope.registered_count() == 0
    assert peer.requests == []
    gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_sdk_refuses_oauth_shape(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        await gate.start(peer.origin)
        key = "fixture-sk-ant-oat-shape"

        with pytest.raises(BridgeRefused) as failure:
            await gate.key(kora_capture, key)
        await gate.release(natural=True)

    assert failure.value.code == "credential"
    assert peer.requests == []
    assert key not in repr(kora_capture.finish())
    gate.assert_reaped()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback", [False, True], ids=["changed-bytes", "ancestor-module"]
)
async def test_pi_sdk_rechecks_staged_authority_before_key(
    copied_stage: Path, tmp_path: Path, kora_capture: Capture, fallback: bool
) -> None:
    root = tmp_path.resolve()
    clock = SystemClock()
    saved = verify_pi_installation(
        copied_stage, clock.monotonic_ns() + 15 * SECOND, clock
    )
    async with model_peer() as peer, witness(saved, root / "attempt") as gate:
        await gate.start(peer.origin)
        if fallback:
            (root / "node_modules").mkdir()
        else:
            saved.bridge.write_text("changed")

        with pytest.raises(InstallationRefused):
            await gate.key(kora_capture)
        await gate.release(natural=False)

    assert kora_capture.scope.registered_count() == 0
    assert peer.requests == []
    gate.assert_reaped()


class ObservedBashPolicy(AllowDenyPolicy):
    def __init__(self, release: asyncio.Event | None = None) -> None:
        super().__init__()
        self.requests: list[ToolCallRequest] = []
        self.decisions: list[PolicyDecision] = []
        self.entered: asyncio.Event = asyncio.Event()
        self.cancelled: asyncio.Event = asyncio.Event()
        self.release = release

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        self.requests.append(request)
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            decision = await super().decide(request, layers, protected_paths, ctx)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.decisions.append(decision)
        return decision


class BashQualification:
    def __init__(
        self,
        gate: PiGate,
        scoped: ScopedShell,
        checked_policy: ObservedBashPolicy,
    ) -> None:
        self.gate = gate
        self.scoped = scoped
        self.policy = checked_policy
        self.results: list[str] = []
        self.on_admission_check: Callable[[], None] | None = None

    async def dispatch(
        self,
        command: str,
        operation: OperationId,
        cutoff: int,
        current: Callable[[], bool],
    ) -> str:
        def still_admitted() -> bool:
            if self.on_admission_check is not None:
                self.on_admission_check()
            return current()

        try:
            result = await self.scoped.run(
                ScopedShellAdmission(command, operation, cutoff, still_admitted)
            )
        except ScopedShellError:
            raise ShellCallRefused() from None
        encoded = json.dumps(
            {"exit_code": result.exit_code, "stdout": result.stdout},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.results.append(encoded)
        return encoded


@asynccontextmanager
async def bash_witness(
    installation: PiInstallation,
    directory: Path,
    capture: Capture,
    chosen_layers: PolicyLayers | None = None,
    *,
    protected: tuple[str, ...] = (),
    policy_release: asyncio.Event | None = None,
) -> AsyncGenerator[BashQualification]:
    reaper = AdoptedReaper(grace_ns=0, interval=0.02)
    with subreaper():
        reaper.start()
        try:
            async with witness(installation, directory, reaper=reaper) as gate:
                content = BASH_PATH.read_bytes()
                pin = ExecutablePin(len(content), hashlib.sha256(content).hexdigest())
                executable = verify_installed_executable(
                    BASH_PATH, pin, gate.cutoff_ns, gate.clock
                )
                shell_home = directory / "shell-home"
                shell_tmp = directory / "shell-temp"
                shell_home.mkdir()
                shell_tmp.mkdir()

                def sdk_sibling() -> tuple[ProcessRegistration, ...]:
                    registration = gate.owner.registration
                    return () if registration is None else (registration,)

                scoped = ScopedShell(
                    gate.checkout,
                    executable,
                    pin,
                    home=shell_home,
                    temporary=shell_tmp,
                    attempt_token=capture.scope.admit(
                        "pi-qualification-attempt-marker"
                    ),
                    secrets=capture.scope,
                    clock=gate.clock,
                    reaper=reaper,
                    siblings=sdk_sibling,
                )
                checked_policy = ObservedBashPolicy(policy_release)
                qualification = BashQualification(gate, scoped, checked_policy)
                gate.shell = PiShellCall(
                    checked_policy,
                    context_for(capture),
                    chosen_layers or layers(),
                    protected,
                    workspace(gate.checkout.path),
                    "graph.node",
                    qualification.dispatch,
                )
                try:
                    yield qualification
                finally:
                    settled = await scoped.settle(
                        gate.clock.monotonic_ns() + PROCESS_CLEANUP_NS
                    )
                    assert settled, json.dumps(
                        [
                            record
                            for record in capture.reader.records()
                            if record.get("event")
                            in (
                                "runtime.local.command_stop",
                                "runtime.local.scoped_shell",
                            )
                        ],
                        sort_keys=True,
                    )
        finally:
            await reaper.stop()


def requested_bash(command: str) -> bytes:
    return tool_stream(MODEL, "bash", {"command": command})


def leading_block(stream: bytes, block: dict[str, object]) -> bytes:
    first, rest = stream.split(b"\n\n", 1)
    shifted = rest.replace(b'"index":0', b'"index":1')
    return (
        first
        + b"\n\n"
        + sse(
            [
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": block},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ]
        )
        + shifted
    )


def pad_with_comments(response: bytes, size: int) -> bytes:
    padding = size - len(response)
    assert padding >= 4
    full, tail = divmod(padding, 3000)
    if tail < 4:
        full -= 1
        tail += 3000
    return (
        (b": " + b"x" * 2996 + b"\n\n") * full
        + b": "
        + b"x" * (tail - 4)
        + b"\n\n"
        + response
    )


@pytest.mark.asyncio
async def test_pi_bash_executes_exact_approved_call_once(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    command = (
        "printf 'one\\n' >> executions; "
        "printf 'shell-output-marker'; "
        "printf 'shell-stderr-marker' >&2; exit 7"
    )
    response = pad_with_comments(requested_bash(command), MODEL_BYTES // 2)
    terminal = pad_with_comments(
        end_stream(MODEL).replace(b'"done"', b'"provider-event-marker"'),
        MODEL_BYTES // 2,
    )
    assert len(response) + len(terminal) == MODEL_BYTES
    async with (
        model_peer(responses=(response, terminal)) as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        continue_response = asyncio.Event()
        peer.continuation_gate = continue_response
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        registration = gate.owner.registration

        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(gate.prompt(PROMPT))
            try:
                async with asyncio.timeout(5):
                    await peer.continuing.wait()
                gate.require_binding()
                assert registration is not None
                assert gate.owner.registration == registration
            finally:
                continue_response.set()
        text = pending.result()
        exit_code = await gate.release(natural=True)

        assert (text, exit_code) == ("provider-event-marker", 0)
        gate.assert_reaped()
        assert not witness_.scoped.occupied
        assert (gate.checkout.path / "executions").read_text() == "one\n"
        assert witness_.policy.requests == [
            ToolCallRequest(
                harness="pi",
                tool="Bash",
                tool_use_id=BASH_CALL_ID,
                agent_id=None,
                arguments={"command": command},
                workspace=workspace(gate.checkout.path),
                node_path="graph.node",
            )
        ]
        assert witness_.policy.decisions == [PolicyDecision(True, "allowed")]
        initial, continuation = peer.requests
        assert initial.authorized
        assert continuation.authorized
        assert initial.document["model"] == continuation.document["model"] == MODEL
        assert initial.document["tools"] == continuation.document["tools"]
        advertised = initial.document["tools"]
        assert isinstance(advertised, list)
        assert len(advertised) == 1
        tool = advertised[0]
        assert isinstance(tool, dict)
        assert tool["name"] == "bash"
        assert tool["input_schema"] == {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        }
        expected = '{"exit_code":7,"stdout":"shell-output-marker"}'
        assert witness_.results == [expected]
        assert continuation.document["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": PROMPT}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": BASH_CALL_ID,
                        "name": "bash",
                        "input": {"command": command},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": BASH_CALL_ID,
                        "content": expected,
                        "is_error": False,
                    }
                ],
            },
        ]
        diagnostics = repr(kora_capture.finish())
        for forbidden in (
            FIXTURE_KEY,
            command,
            "shell-output-marker",
            "shell-stderr-marker",
            "provider-event-marker",
        ):
            assert forbidden not in diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["namespace", "file-rule", "protected-path"])
async def test_pi_bash_namespace_deny_prevents_launch(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, reason: str
) -> None:
    command = "touch effect" if reason == "namespace" else "touch apply_patch"
    namespace = ("Bash(touch *)",) if reason == "namespace" else ("Edit(effect)",)
    chosen = layers(
        namespace=policy(deny=() if reason == "protected-path" else namespace),
        node=policy(allow=("Bash(touch *)",)),
    )
    protected = ("effect",) if reason == "protected-path" else ()
    async with (
        model_peer(responses=(requested_bash(command),)) as peer,
        bash_witness(
            installation, tmp_path, kora_capture, chosen, protected=protected
        ) as witness_,
    ):
        gate = witness_.gate
        files = FixtureFiles(gate.checkout.path)
        await gate.start(peer.origin)
        await gate.key(kora_capture)

        with pytest.raises(BridgeRefused):
            await gate.prompt(PROMPT)
        await gate.release(natural=False)

        assert len(peer.requests) == 1
        assert len(witness_.policy.requests) == 1
        assert witness_.policy.decisions == [
            PolicyDecision(
                False,
                "namespace.deny.0" if reason == "namespace" else "argument_invalid",
            )
        ]
        assert witness_.results == []
        assert not witness_.scoped.occupied
        files.assert_unchanged()
        gate.assert_reaped()


def invalid_bash_response(reason: str, command: str) -> bytes:
    documented = requested_bash(command)
    parts = documented.split(b"\n\n")[:-1]
    start, block, delta, close, stopped, terminal = parts
    match reason:
        case "duplicate-argument":
            parts[2] = delta.replace(
                b'\\"command\\"', b'\\"command\\":\\"touch effect\\",\\"command\\"'
            )
        case "repaired-argument":
            parts[2] = delta.replace(b'}"}', b'"}', 1)
        case "extra-argument":
            parts[2] = delta.replace(b'{\\"command', b'{\\"extra\\":true,\\"command')
        case "duplicate-event-field":
            parts[2] = delta.replace(b'"index":0', b'"index":0,"index":0')
        case "wrong-model":
            parts[0] = start.replace(MODEL.encode(), b"unexpected-model")
        case "duplicate-call":
            parts = [
                start,
                block,
                delta,
                close,
                *(
                    part.replace(b'"index":0', b'"index":1')
                    for part in (block, delta, close)
                ),
                stopped,
                terminal,
            ]
        case "dangling-call":
            parts.remove(close)
        case "unknown-index":
            parts[2] = delta.replace(b'"index":0', b'"index":1')
        case "input-seed":
            parts[1] = block.replace(
                b'"input":{}', b'"input":{"command":"touch effect"}'
            )
        case "wrong-tool":
            parts[1] = block.replace(b'"name":"bash"', b'"name":"other"')
        case "noncanonical-id":
            parts[1] = block.replace(BASH_CALL_ID.encode(), b"call.with.dot")
        case "wrong-stop":
            parts[4] = stopped.replace(b'"tool_use"', b'"end_turn"')
        case "incomplete-frame":
            return documented[:-1]
        case "invalid-utf8":
            return documented + b":\xff\n\n"
        case "partial-then-more":
            return documented + block
        case "credential-command":
            return requested_bash(f"touch effect; printf {FIXTURE_KEY}")
        case _:
            raise AssertionError(reason)
    changed = b"\n\n".join(parts) + b"\n\n"
    assert changed != documented
    return changed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "duplicate-argument",
        "repaired-argument",
        "extra-argument",
        "duplicate-event-field",
        "wrong-model",
        "duplicate-call",
        "dangling-call",
        "unknown-index",
        "input-seed",
        "wrong-tool",
        "noncanonical-id",
        "wrong-stop",
        "incomplete-frame",
        "invalid-utf8",
        "partial-then-more",
        "credential-command",
    ],
)
async def test_pi_bash_refuses_unbound_or_abandoned_call(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, reason: str
) -> None:
    response = invalid_bash_response(reason, "touch effect")
    async with (
        model_peer(responses=(response,)) as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        effect = gate.checkout.path / "effect"
        await gate.start(peer.origin)
        await gate.key(kora_capture)

        with pytest.raises(BridgeRefused):
            await gate.prompt(PROMPT)
        await gate.release(natural=False)

        assert len(peer.requests) == 1
        assert peer.connections == 1
        assert witness_.policy.requests == witness_.results == []
        assert not witness_.scoped.occupied
        assert not effect.exists()
        gate.assert_reaped()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["cancellation", "lost-peer", "stale-allow"])
async def test_pi_bash_abandons_pending_policy_before_dispatch(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, revocation: str
) -> None:
    policy_release = asyncio.Event()
    response = requested_bash("touch effect")
    async with (
        model_peer(responses=(response,)) as peer,
        bash_witness(
            installation, tmp_path, kora_capture, policy_release=policy_release
        ) as witness_,
    ):
        gate = witness_.gate
        effect = gate.checkout.path / "effect"
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        pending = asyncio.create_task(gate.prompt(PROMPT))
        try:
            async with asyncio.timeout(5):
                await witness_.policy.entered.wait()

            if revocation == "cancellation":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                if revocation == "lost-peer":
                    gate.owner.signal(signal.SIGKILL, gate.cutoff_ns)
                else:
                    gate.stop()
                    policy_release.set()
                with pytest.raises(BridgeRefused):
                    await pending
        finally:
            policy_release.set()
            if not pending.done():
                pending.cancel()
                await observed(pending)
        await gate.release(natural=False)

        assert len(peer.requests) == 1
        assert len(witness_.policy.requests) == 1
        if revocation == "stale-allow":
            assert witness_.policy.decisions == [PolicyDecision(True, "allowed")]
        else:
            assert witness_.policy.cancelled.is_set()
        assert witness_.results == []
        assert not witness_.scoped.occupied
        assert not effect.exists()
        gate.assert_reaped()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "cumulative-overflow",
        "missing-start",
        "missing-delimiter",
        "another-call",
        "wrong-model",
    ],
)
async def test_pi_bash_requires_complete_continued_response(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, reason: str
) -> None:
    first = requested_bash("printf one >> effects")
    second = end_stream(MODEL)
    if reason == "cumulative-overflow":
        padding = (b": " + b"x" * 2996 + b"\n\n") * 44
        first, second = padding + first, padding + second
        assert max(len(first), len(second)) < MODEL_BYTES < len(first) + len(second)
    elif reason == "missing-start":
        _, second = second.split(b"\n\n", 1)
    elif reason == "missing-delimiter":
        second = second[:-1]
    elif reason == "another-call":
        second = first
    else:
        second = end_stream("unexpected-model")
    async with (
        model_peer(responses=(first, second)) as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        await gate.key(kora_capture)

        with pytest.raises(BridgeRefused):
            await gate.prompt(PROMPT)
        await gate.release(natural=False)

        assert len(peer.requests) == 2
        assert len(witness_.policy.requests) == len(witness_.results) == 1
        assert (gate.checkout.path / "effects").read_text() == "one"
        assert not witness_.scoped.occupied
        gate.assert_reaped()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["initial", "text", "thinking"])
async def test_pi_bash_rejects_credential_in_model_request_body(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, location: str
) -> None:
    key = MODEL if location == "initial" else 'private-echo-"quoted"-credential'
    first = requested_bash("printf one >> effects")
    if location == "text":
        first = leading_block(first, {"type": "text", "text": key})
    elif location == "thinking":
        first = leading_block(
            first,
            {"type": "thinking", "thinking": key, "signature": "fixture-signature"},
        )
    async with (
        model_peer(responses=(first,), key=key) as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        await gate.key(kora_capture, key)

        with pytest.raises(BridgeRefused):
            await gate.prompt(PROMPT)
        await gate.release(natural=False)

        expected = int(location != "initial")
        assert peer.connections == peer.header_count == len(peer.requests) == expected
        assert len(witness_.policy.requests) == len(witness_.results) == expected
        assert (gate.checkout.path / "effects").exists() == bool(expected)
        assert not witness_.scoped.occupied
        if location != "initial":
            assert key not in repr(kora_capture.finish())
        gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_bash_refuses_oversized_outbound_body_before_transfer(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    prompt = "p" * 32_768
    echo = "t" * (240 * 1024)
    first = leading_block(
        requested_bash("printf one >> effects"), {"type": "text", "text": echo}
    )
    assert len(first) < MODEL_BYTES < len(echo) + len(prompt)
    async with (
        model_peer(responses=(first,)) as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        await gate.key(kora_capture)

        with pytest.raises(BridgeRefused):
            await gate.prompt(prompt)
        await gate.release(natural=False)

        assert peer.connections == peer.header_count == len(peer.requests) == 1
        assert len(witness_.policy.requests) == len(witness_.results) == 1
        assert (gate.checkout.path / "effects").read_text() == "one"
        assert not witness_.scoped.occupied
        gate.assert_reaped()


async def assert_stopped_shell(
    installation: PiInstallation,
    path: Path,
    capture: Capture,
    lose_sdk: bool,
) -> None:
    script = DESCENDANT.replace(
        "record('ready', 1)", "record('effect', 1)\nrecord('ready', 1)"
    )
    async with (
        model_peer(responses=(requested_bash(python_command(script)),)) as peer,
        bash_witness(installation, path, capture) as witness_,
    ):
        gate = witness_.gate
        workspace_ = gate.checkout.path
        await gate.start(peer.origin)
        await gate.key(capture)
        registration = gate.owner.registration
        pending = asyncio.create_task(gate.prompt(PROMPT))
        try:
            await until(
                lambda: (
                    pending.done()
                    or (
                        (workspace_ / "ready").exists()
                        and (workspace_ / "descendant").exists()
                    )
                )
            )
            assert not pending.done()
            assert witness_.scoped.occupied
            assert (workspace_ / "effect").read_text() == "1"
            descendant = current_identity(workspace_ / "descendant")
            leader = current_identity(workspace_ / "leader")
            native = gate.identities.read(descendant.pid).process
            assert native is not None
            assert native.process_group_id != leader.pid

            if lose_sdk:
                gate.owner.signal(signal.SIGKILL, gate.cutoff_ns)
                assert not gate.stopped
            else:
                gate.stop()
            with pytest.raises(BridgeRefused) as refused:
                await pending
            assert not witness_.scoped.occupied
            exit_status = await gate.release(natural=False)
        finally:
            if not pending.done():
                pending.cancel()
                await observed(pending)

        assert refused.value.code == "closed"
        if lose_sdk:
            assert exit_status == -signal.SIGKILL
        else:
            assert exit_status in (1, -signal.SIGTERM, -signal.SIGKILL)
        assert gate.owner.registration == registration
        assert_gone(leader)
        assert_gone(descendant)
        assert not witness_.scoped.occupied
        assert len(witness_.policy.requests) == 1
        assert witness_.results == []
        assert len(peer.requests) == 1
        assert (workspace_ / "effect").read_text() == "1"
        assert not (workspace_ / "executions").exists()
        gate.assert_reaped()


@pytest.mark.asyncio
async def test_pi_bash_stop_reaps_descendant_and_closes_io(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    await assert_stopped_shell(installation, tmp_path, kora_capture, lose_sdk=False)


@pytest.mark.asyncio
async def test_pi_bash_lost_result_retains_original_owner(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    await assert_stopped_shell(installation, tmp_path, kora_capture, lose_sdk=True)


BASH_IO_PROBE: Final[str] = """
import os
import stat
import sys
from pathlib import Path
assert os.read(0, 1) == b''
assert stat.S_ISCHR(os.fstat(0).st_mode)
assert stat.S_ISFIFO(os.fstat(1).st_mode)
assert stat.S_ISFIFO(os.fstat(2).st_mode)
assert os.fstat(1).st_ino != os.fstat(2).st_ino
try:
    os.fstat(int(sys.argv[1]))
except OSError:
    pass
else:
    raise RuntimeError('unexpected inherited descriptor')
assert not any('ANTHROPIC' in key or 'PI_' in key for key in os.environ)
fixture_key = 'pi-sdk-scripted-' + 'credential-9f71'
assert not any(fixture_key in value for value in os.environ.values())
assert Path(os.environ['HOME']).name == 'shell-home'
assert Path(os.environ['TMPDIR']).name == 'shell-temp'
assert os.environ['PWD'] == os.getcwd()
assert os.environ['PATH'] == '/usr/bin:/bin'
assert len(os.environ['KORA_SCOPED_SHELL']) == 32
print('fixture-stderr-marker', file=sys.stderr)
os.write(1, Path('output').read_bytes())
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["settled", "raw-overflow", "encoded-overflow"])
async def test_pi_bash_preserves_scoped_shell_io_bounds(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, outcome: str
) -> None:
    async with (
        model_peer() as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        assert gate.channel is not None
        info = cast(socket.socket, gate.streams[1].get_extra_info("socket"))
        descriptor = fcntl.fcntl(info.fileno(), fcntl.F_DUPFD, 129)
        private_text = "independent-output-mask"
        kora_capture.scope.admit(private_text)
        output = {
            "settled": private_text.encode() + b"\xff",
            "raw-overflow": b"x" * (OUTPUT_BYTES + 1),
            "encoded-overflow": b'"' * (OUTPUT_BYTES // 2),
        }[outcome]
        (gate.checkout.path / "output").write_bytes(output)
        peer.responses = (
            requested_bash(python_command(BASH_IO_PROBE, str(descriptor))),
            end_stream(MODEL),
        )
        try:
            if outcome == "settled":
                assert await gate.prompt(PROMPT) == "done"
            else:
                with pytest.raises(BridgeRefused):
                    await gate.prompt(PROMPT)
        finally:
            os.close(descriptor)
        await gate.release(natural=outcome == "settled")

        if outcome == "settled":
            assert witness_.results == ['{"exit_code":0,"stdout":"[redacted]�"}']
            assert len(peer.requests) == 2
        else:
            assert witness_.results == []
            assert len(peer.requests) == 1
        assert len(witness_.policy.requests) == 1
        assert not witness_.scoped.occupied
        assert private_text not in repr(kora_capture.finish())
        gate.assert_reaped()


def bash_call_frame(challenge: str, /, **overrides: str) -> bytes:
    return (
        json.dumps(
            {
                "kind": "bash_call",
                "nonce": NONCE,
                "challenge": challenge,
                "id": BASH_CALL_ID,
                "call": "c" * 32,
                "command": "printf one >> effects",
            }
            | overrides,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_field", ["nonce", "challenge", "call", "id", "terminal"]
)
async def test_pi_bash_refuses_wrong_control_binding(
    installation: PiInstallation,
    tmp_path: Path,
    kora_capture: Capture,
    wrong_field: str,
) -> None:
    async with (
        bash_witness(installation, tmp_path, kora_capture) as witness_,
        model_peer() as peer,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        async with prepared_channel(kora_capture, StepClock(), shell=gate.shell) as (
            channel,
            reader,
            streams,
            _,
        ):
            pending = asyncio.create_task(channel.prompt(PROMPT))
            async with asyncio.timeout(5):
                challenge = await control_challenge(streams[0], "prompt")

            if wrong_field == "terminal":
                frame = bridge_reply("terminal", challenge)
            else:
                value = {
                    "nonce": "a" * 32,
                    "challenge": "b" * 32,
                    "call": challenge,
                    "id": "a.b",
                }[wrong_field]
                frame = bash_call_frame(challenge, **{wrong_field: value})
            reader.feed_data(frame)
            with pytest.raises(BridgeRefused) as refused:
                await pending

        assert refused.value.code == "protocol"
        assert witness_.policy.requests == witness_.results == []
        assert not (gate.checkout.path / "effects").exists()


class WriteObservationClock(SystemClock):
    def __init__(self) -> None:
        self.waiting = asyncio.Event()

    async def sleep_until(self, deadline_ns: int) -> None:
        self.waiting.set()
        await super().sleep_until(deadline_ns)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["duplicate", "stop"])
async def test_pi_bash_revokes_blocked_result_without_replay(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, revocation: str
) -> None:
    clock = WriteObservationClock()
    async with (
        bash_witness(installation, tmp_path, kora_capture) as witness_,
        model_peer() as peer,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        async with prepared_channel(kora_capture, clock, shell=gate.shell) as (
            channel,
            reader,
            streams,
            _,
        ):
            peer_reader, peer_writer = streams
            transport = peer_writer.transport
            assert isinstance(transport, asyncio.Transport)
            pending = asyncio.create_task(channel.prompt(PROMPT))
            async with asyncio.timeout(5):
                challenge = await control_challenge(peer_reader, "prompt")
                transport.pause_reading()
                command = python_command(
                    "from pathlib import Path; Path('effects').write_text('one'); "
                    "print('x' * 6000)"
                )
                frame = bash_call_frame(challenge, command=command)
                reader.feed_data(frame)
                await clock.waiting.wait()

                if revocation == "duplicate":
                    reader.feed_data(frame)
                else:
                    channel.revoke()
                with pytest.raises(BridgeRefused) as refused:
                    await pending
                transport.resume_reading()
                partial = await peer_reader.read()

        assert refused.value.code == (
            "protocol" if revocation == "duplicate" else "closed"
        )
        assert partial.startswith(b'{"kind":"bash_result"')
        assert b"\n" not in partial
        assert len(witness_.policy.requests) == len(witness_.results) == 1
        assert (gate.checkout.path / "effects").read_text() == "one"
        assert not witness_.scoped.occupied
        assert peer.requests == []


@pytest.mark.asyncio
async def test_pi_bash_stop_after_shell_preparation_refuses_activation(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    first = requested_bash("printf marker > effect")
    async with (
        model_peer(responses=(first,)) as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        baseline = gate_descriptors()
        effect = gate.checkout.path / "effect"
        checks = 0

        def before_activation() -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                assert witness_.scoped.occupied
                assert len(gate_descriptors()) > len(baseline)
                gate.stop()

        witness_.on_admission_check = before_activation

        with pytest.raises(BridgeRefused):
            await gate.prompt(PROMPT)
        await gate.release(natural=False)

        assert checks == 2
        assert not effect.exists()
        assert not witness_.scoped.occupied
        assert len(witness_.policy.requests) == 1
        assert witness_.results == []
        assert len(peer.requests) == 1
        gate.assert_reaped()


def control_wire(kind: str, nonce: str, **fields: str) -> bytes:
    return (
        json.dumps(
            {"kind": kind, "nonce": nonce, **fields}, separators=(",", ":")
        ).encode()
        + b"\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_field", ["id", "call", "challenge", "activation"])
async def test_pi_bash_sdk_refuses_unbound_result(
    installation: PiInstallation,
    tmp_path: Path,
    kora_capture: Capture,
    wrong_field: str,
) -> None:
    response = requested_bash("touch effect")
    async with (
        model_peer(responses=(response,)) as peer,
        bash_witness(installation, tmp_path, kora_capture) as witness_,
    ):
        gate = witness_.gate
        await gate.start(peer.origin)
        await gate.key(kora_capture)
        reader, writer = gate.streams
        challenge = new_operation_id()
        writer.write(
            control_wire("prompt", gate.nonce, text=PROMPT)
            + control_wire("activate", gate.nonce, challenge=challenge)
        )
        async with asyncio.timeout(5):
            request = decode_strict_json(
                await reader.readuntil(b"\n"), MAX_FRAME_BYTES, 1
            )
        assert isinstance(request, dict)
        assert request["kind"] == "bash_call"
        assert request["id"] == BASH_CALL_ID
        assert request["challenge"] == challenge
        operation = request["call"]
        assert isinstance(operation, str)
        result_fields = {
            "id": BASH_CALL_ID,
            "call": operation,
            "challenge": challenge,
            "result": '{"exit_code":0,"stdout":"fixture"}',
        }
        activation = operation
        if wrong_field == "activation":
            activation = new_operation_id()
        else:
            result_fields[wrong_field] = new_operation_id()

        writer.write(
            control_wire("bash_result", gate.nonce, **result_fields)
            + control_wire("activate", gate.nonce, challenge=activation)
        )
        async with asyncio.timeout(5):
            refused = decode_strict_json(
                await reader.readuntil(b"\n"), MAX_FRAME_BYTES, 1
            )
        exit_status = await gate.release(natural=True)

        assert refused == {
            "kind": "failure",
            "code": "protocol",
            "nonce": gate.nonce,
            "challenge": challenge,
        }
        assert exit_status == 1
        assert len(peer.requests) == 1
        assert witness_.policy.requests == witness_.results == []
        assert not (gate.checkout.path / "effect").exists()
        gate.assert_reaped()
