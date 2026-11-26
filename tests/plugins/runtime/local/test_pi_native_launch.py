import asyncio
import os
import shutil
import signal
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import decode_strict_json
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
from kora.plugins.runtime.local.bound_checkout import BoundCheckout, BoundCheckoutError
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.identity import NativeIdentityReader
from kora.runner.process_stdio import StdioStreams
from kora.runner.processes import GatedProcess, ProcessCondition, start_gated
from tests.plugins.harness.claude.scripted_model import end_stream
from tests.plugins.harness.codex.selected_gated_stdio import native_environment

__all__ = ["kora_capture"]

SECOND: Final[int] = 1_000_000_000
MODEL: Final[str] = "claude-sonnet-4-5-20250929"
FIXTURE_KEY: Final[str] = "pi-sdk-scripted-credential-9f71"
PROMPT: Final[str] = "Produce the fixed witness value."
AMBIENT: Final[str] = "PI_DISCOVERY_CANARY_4ccbd"
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
        overflow: bool = False,
        response_gate: asyncio.Event | None = None,
        reported_model: str = MODEL,
    ) -> None:
        self.hold: bool = hold
        self.redirect: str | None = redirect
        self.overflow: bool = overflow
        self.response_gate: asyncio.Event | None = response_gate
        self.reported_model: str = reported_model
        self.received: asyncio.Event = asyncio.Event()
        self.requests: list[Request] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.origin: str = ""

    def accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
                length = int(fields.get("content-length", "0"))
                if not 0 < length <= MAX_FRAME_BYTES:
                    return
                document = decode_strict_json(
                    await reader.readexactly(length), MAX_FRAME_BYTES, 16
                )
                if not isinstance(document, dict):
                    return
                self.requests.append(
                    Request(
                        headers[0], fields.get("x-api-key") == FIXTURE_KEY, document
                    )
                )
                self.received.set()
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
                    body = (
                        b"data: " + b"x" * (256 * 1024)
                        if self.overflow
                        else end_stream(self.reported_model)
                    )
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
                if not self.hold and self.response_gate is None:
                    raise


@asynccontextmanager
async def model_peer(
    *,
    hold: bool = False,
    redirect: str | None = None,
    overflow: bool = False,
    response_gate: asyncio.Event | None = None,
    reported_model: str = MODEL,
) -> AsyncGenerator[ModelPeer]:
    peer = ModelPeer(
        hold=hold,
        redirect=redirect,
        overflow=overflow,
        response_gate=response_gate,
        reported_model=reported_model,
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


@pytest.fixture
def stage_path() -> Path:
    return Path(os.environ["KORA_PI_SDK_STAGE"])


@pytest.fixture
def installation(stage_path: Path) -> PiInstallation:
    clock = SystemClock()
    return verify_pi_installation(stage_path, clock.monotonic_ns() + 15 * SECOND, clock)


def seed_discovery(root: Path) -> None:
    settings = root / ".pi"
    settings.mkdir(mode=0o700)
    (settings / "settings.json").write_text('{"defaultModel":"' + AMBIENT + '"}')
    (root / "AGENTS.md").write_text(AMBIENT)
    extensions = settings / "extensions"
    extensions.mkdir()
    (extensions / "canary.ts").write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "export default function check() {\n"
        "  writeFileSync('pi-extension-loaded', '1');\n"
        "}\n"
    )
    (settings / "commands").mkdir()


class PiGate:
    def __init__(
        self, installation: PiInstallation, checkout: BoundCheckout, home: Path
    ) -> None:
        self.installation: PiInstallation = installation
        self.checkout: BoundCheckout = checkout
        self.clock: SystemClock = SystemClock()
        self.cutoff_ns: int = self.clock.monotonic_ns() + 10 * SECOND
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
            new_operation_id(),
            checkout.anchor.descriptor,
            self.cutoff_ns,
            self.clock,
            stdio_limit=MAX_FRAME_BYTES - 1,
        )
        self.channel: BridgeChannel
        self.streams: StdioStreams
        self.pid: int

    async def start(self, origin: str, model: str = MODEL) -> None:
        registration = await self.owner.prepare()
        self.pid = registration.process.pid
        self.checkout.verify(self.checkout.path, self.cutoff_ns, self.clock)
        await self.owner.acknowledge()
        self.streams = self.owner.borrow_stdio()
        self.channel = BridgeChannel(self.streams, NONCE, self.cutoff_ns, self.clock)
        await self.channel.initialize(origin, model)

    def require_live(self) -> None:
        self.checkout.verify(self.checkout.path, self.cutoff_ns, self.clock)
        if self.owner.observe(self.cutoff_ns).condition is not ProcessCondition.LIVE:
            raise BridgeRefused("stopped")
        self.owner.require_acquired()

    async def key(self, capture: Capture) -> None:
        await asyncio.to_thread(self.installation.recheck, self.cutoff_ns, self.clock)
        self.require_live()
        await self.channel.credential(capture.scope.admit(FIXTURE_KEY))

    async def prompt(self, text: str) -> str:
        self.require_live()
        return await self.channel.prompt(text)

    async def release(self, *, natural: bool) -> int:
        cutoff_ns = self.clock.monotonic_ns() + 6 * SECOND
        self.owner.close_gate()
        if natural:
            await self.owner.settle(cutoff_ns)
        else:
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
        return self.owner.release(cutoff_ns)

    def assert_reaped(self) -> None:
        assert self.owner.closed
        assert self.streams[1].is_closing()
        with pytest.raises(ChildProcessError):
            os.waitpid(self.pid, os.WNOHANG)


@asynccontextmanager
async def witness(
    installation: PiInstallation, directory: Path
) -> AsyncGenerator[PiGate]:
    root = directory.resolve()
    checkout_path = root / "checkout"
    checkout_path.mkdir(mode=0o700)
    seed_discovery(checkout_path)
    home = root / "role"
    home.mkdir(mode=0o700)
    seed_discovery(home)
    ambient_agent = home / ".pi" / "agent"
    ambient_agent.mkdir()
    (ambient_agent / "auth.json").write_text(
        '{"anthropic":{"type":"api_key","key":"' + AMBIENT + '"}}'
    )
    checkout = BoundCheckout.capture(checkout_path)
    try:
        owner = PiGate(installation, checkout, home)
        try:
            yield owner
        finally:
            if not owner.owner.closed:
                await owner.release(natural=False)
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

        text = await gate.prompt(PROMPT)
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
        assert PROMPT in repr(request.document)
        assert AMBIENT not in repr(request.document)
        assert FIXTURE_KEY not in repr(request.document)
        assert FIXTURE_KEY not in repr(gate.recipe)
        assert FIXTURE_KEY not in repr(kora_capture.finish())
        assert arguments == tuple(os.fsencode(value) for value in gate.recipe.argv)
        if environment is not None:
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
@pytest.mark.parametrize("overflow", [False, True], ids=["redirect", "oversized-body"])
async def test_pi_sdk_refuses_unadmitted_provider_response(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture, overflow: bool
) -> None:
    async with model_peer() as destination:
        async with (
            model_peer(
                overflow=overflow, redirect=None if overflow else destination.origin
            ) as selected,
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


@pytest.mark.parametrize("replacement", ["changed", "symlink", "late"])
def test_pi_installation_refuses_changed_entry(
    stage_path: Path, tmp_path: Path, replacement: str
) -> None:
    copied = tmp_path.resolve() / "stage"
    shutil.copytree(stage_path, copied)
    entry = copied / "installed/bridge/dist/bridge.js"
    clock = SystemClock()
    cutoff_ns = clock.monotonic_ns() + 15 * SECOND
    admission = (
        verify_pi_installation(copied, cutoff_ns, clock)
        if replacement == "late"
        else None
    )
    if replacement in ("changed", "late"):
        entry.write_bytes(b"refused")
    else:
        entry.unlink()
        entry.symlink_to(stage_path / "installed/bridge/dist/bridge.js")
    check = (
        partial(verify_pi_installation, copied, cutoff_ns, clock)
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

        assert failure == {"kind": "failure", "nonce": NONCE, "code": "protocol"}
        assert result == 1
        gate.assert_reaped()
        assert peer.requests == []


@pytest.mark.asyncio
async def test_pi_sdk_refuses_failed_startup(
    installation: PiInstallation, tmp_path: Path
) -> None:
    async with model_peer() as peer, witness(installation, tmp_path) as gate:
        with pytest.raises(BridgeRefused) as failure:
            await gate.start(peer.origin, "fixture-model-not-in-catalog")
        result = await gate.release(natural=True)

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

        await gate.release(natural=False)
        with pytest.raises(BridgeRefused, match="pi_bridge_refused") as failure:
            await action()

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

        gate.owner.close_gate()
        send_response.set()
        with pytest.raises(BridgeRefused) as failure:
            await prompt
        await gate.release(natural=False)

    assert failure.value.code == "closed"
    gate.assert_reaped()
    assert len(peer.requests) == 1


@pytest.mark.asyncio
async def test_pi_bridge_closes_with_pending_control_write() -> None:
    socket_end, held = socket.socketpair()
    with socket_end, held:
        socket_end.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        streams = await asyncio.open_connection(sock=socket_end)
        clock = SystemClock()
        channel = BridgeChannel(
            streams, NONCE, clock.monotonic_ns() + 5 * SECOND, clock
        )
        _, writer = streams
        started = asyncio.Event()

        async def send() -> str:
            started.set()
            return await channel.prompt("x" * 32_768)

        pending = asyncio.create_task(send())
        await started.wait()
        try:
            assert writer.transport.get_write_buffer_size() > 0
            writer.transport.abort()
            with pytest.raises(BridgeRefused):
                await pending
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_pi_sdk_refuses_reported_model_mismatch(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    async with (
        model_peer(reported_model="different-physical-model") as peer,
        witness(installation, tmp_path) as gate,
    ):
        await gate.start(peer.origin)
        await gate.key(kora_capture)

        with pytest.raises(BridgeRefused) as failure:
            await gate.prompt(PROMPT)
        result = await gate.release(natural=True)

    assert failure.value.code == "prompt"
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
        prefix = f'{{"kind":"credential","nonce":"{NONCE}","key":"synthetic'

        writer.write(prefix.encode())
        await writer.drain()
        await gate.release(natural=False)

        assert await reader.read() == b""
        assert peer.requests == []
        gate.assert_reaped()
