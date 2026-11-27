import asyncio
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
from kora.conformance.common import CONFORMANCE_COMPONENT, INSTANCE_ID, utc_now
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
from kora.plugins.runtime.local.bound_checkout import BoundCheckout, BoundCheckoutError
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    new_operation_id,
)
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.child_wait import Readiness, native_direct_child_wait
from kora.runner.identity import NativeIdentityReader
from kora.runner.native_io import observed
from kora.runner.process_stdio import StdioStreams
from kora.runner.processes import GatedProcess, start_gated
from tests.plugins.harness.claude.scripted_model import end_stream
from tests.plugins.harness.codex.selected_gated_stdio import native_environment
from tests.plugins.runtime.local.frames_support import StepClock

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
        padding_records: int = 0,
        response_gate: asyncio.Event | None = None,
        reported_model: str = MODEL,
        reported_stop: str = "end_turn",
    ) -> None:
        self.hold: bool = hold
        self.redirect: str | None = redirect
        self.padding_records: int = padding_records
        self.response_gate: asyncio.Event | None = response_gate
        self.reported_model: str = reported_model
        self.reported_stop: str = reported_stop
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
                    comment = b": " + b"x" * 2996 + b"\n\n"
                    body = comment * self.padding_records + end_stream(
                        self.reported_model
                    ).replace(b'"end_turn"', f'"{self.reported_stop}"'.encode())
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
) -> AsyncGenerator[ModelPeer]:
    peer = ModelPeer(
        hold=hold,
        redirect=redirect,
        padding_records=padding_records,
        response_gate=response_gate,
        reported_model=reported_model,
        reported_stop=reported_stop,
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
def kora_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(level=Level.DEBUG), INSTANCE_ID, utc_now
    ) as capture:
        yield capture


@pytest.fixture
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
    ) -> None:
        self.installation: PiInstallation = installation
        self.checkout: BoundCheckout = checkout
        self.clock: SystemClock = SystemClock()
        self._scan_clock: Clock = self.clock if scan_clock is None else scan_clock
        self._installation_scan: asyncio.Task[None] | None = None
        self._release_task: asyncio.Task[int] | None = None
        self.identities = NativeIdentityReader(clock=self.clock)
        self._child_probe, _ = native_direct_child_wait()
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
        )
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

    async def release(self, *, natural: bool) -> int:
        release = self._begin_release(natural=natural)
        cancelled = await observed(release)
        if self._release_task is release and not self.owner.closed:
            self._release_task = None
        if cancelled:
            raise asyncio.CancelledError
        return release.result()

    async def _settle_release(self, *, natural: bool) -> int:
        cutoff_ns = self.clock.monotonic_ns() + 6 * SECOND
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
        return self.owner.release(cutoff_ns)

    def assert_reaped(self) -> None:
        assert self.owner.closed
        assert self.channel is None or self.streams[1].is_closing()
        with pytest.raises(ChildProcessError):
            os.waitpid(self.pid, os.WNOHANG)


@asynccontextmanager
async def witness(
    installation: PiInstallation, directory: Path, *, scan_clock: Clock | None = None
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
        owner = PiGate(installation, checkout, home, scan_clock)
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
        transport = gate.streams[1].transport
        assert isinstance(transport, asyncio.Transport)

        transport.pause_reading()
        pending = asyncio.create_task(gate.prompt(PROMPT))
        try:
            await gate.owner.wait_exit(gate.cutoff_ns)
        finally:
            transport.resume_reading()
        text = await pending
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
    clock: StepClock,
    require_binding: Callable[[bool], None] | None = None,
    recheck_installation: Callable[[], Awaitable[None]] | None = None,
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
async def test_pi_bridge_stop_invalidates_buffered_terminal(
    kora_capture: Capture,
) -> None:
    async with prepared_channel(kora_capture, StepClock()) as (
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

        reader.feed_data(bridge_reply("terminal", challenge))
        channel.revoke()
        with pytest.raises(BridgeRefused) as stopped:
            await pending

    assert duplicate.value.code == "protocol"
    assert stopped.value.code == "closed"


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
                assert self.resume.wait(5)
        return super().monotonic_ns()


@pytest.mark.asyncio
async def test_pi_sdk_cancellation_joins_entered_installation_scan(
    installation: PiInstallation, tmp_path: Path, kora_capture: Capture
) -> None:
    scan_clock = HeldInstallationClock()
    role = tmp_path / "role"
    async with (
        model_peer() as peer,
        witness(installation, tmp_path, scan_clock=scan_clock) as gate,
    ):
        await gate.start(peer.origin)
        scan_clock.hold_next_scan()

        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(gate.key(kora_capture))
            try:
                async with asyncio.timeout(5):
                    await scan_clock.entered.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                await gate.owner.wait_exit(gate.clock.monotonic_ns() + 4 * SECOND)

                assert gate.stopped
                assert not gate.owner.closed
                assert role.is_dir()
            finally:
                scan_clock.resume.set()
        await gate.release(natural=False)

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
