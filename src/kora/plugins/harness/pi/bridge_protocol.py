import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal, Protocol, cast

from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import decode_strict_json
from kora.plugins.harness.pi.shell_call import (
    BashRequest,
    PiShellCall,
    ShellCallRefused,
)
from kora.protocols.lifetime import new_operation_id
from kora.protocols.secret_value import SecretValue
from kora.protocols.time import Clock
from kora.runner.channels import ChannelError, DescriptorReadiness, wait_future
from kora.runner.native_io import observed
from kora.runner.process_stdio import StdioStreams

MAX_FRAME_BYTES: Final[int] = 65_536
MAX_PROMPT_BYTES: Final[int] = 32_768
MAX_COMMAND_BYTES: Final[int] = 32_768
MAX_RESULT_BYTES: Final[int] = 8_192
_CALL_ID: Final[re.Pattern[str]] = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_NONCE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_FAILURES: Final = ("protocol", "startup", "credential", "prompt", "stopped")
Command = Literal["init", "credential", "prompt"]
Reply = Literal["ready", "credential_ready", "terminal"]


class _Endpoint(Protocol):
    def fileno(self) -> int: ...


class BridgeRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("pi_bridge_refused")


class BridgeChannel:
    def __init__(
        self,
        streams: StdioStreams,
        nonce: str,
        cutoff_ns: int,
        clock: Clock,
        require_binding: Callable[[bool], None],
        recheck_installation: Callable[[], Awaitable[None]],
        shell: PiShellCall | None = None,
        on_revoked: Callable[[], None] | None = None,
    ) -> None:
        if _NONCE.fullmatch(nonce) is None:
            raise BridgeRefused("protocol")
        self._reader, self._writer = streams
        self._endpoint = cast(_Endpoint, self._writer.get_extra_info("socket"))
        self._nonce = nonce
        self._cutoff_ns = cutoff_ns
        self._clock = clock
        self._require_binding = require_binding
        self._recheck_installation = recheck_installation
        self._shell = shell
        self._on_revoked = on_revoked
        self._phase: Command | None = "init"
        self._revoked: bool = False
        self._waiting: tuple[int, DescriptorReadiness] | None = None

    async def initialize(self, origin: str, model: str) -> None:
        profile = (
            {}
            if self._shell is None
            else {"profile": "bash", "cutoff": str(self._native_cutoff())}
        )
        await self._exchange(
            "init", "ready", "credential", origin=origin, model=model, **profile
        )

    async def credential(self, key: SecretValue) -> None:
        await self._exchange("credential", "credential_ready", "prompt", key)

    async def prompt(self, text: str) -> str:
        if len(text) > MAX_PROMPT_BYTES or len(text.encode()) > MAX_PROMPT_BYTES:
            raise BridgeRefused("protocol")
        return await self._exchange("prompt", "terminal", None, text=text)

    def revoke(self) -> None:
        if self._revoked:
            return
        self._revoked = True
        self._release_waiter()
        self._writer.transport.abort()
        if self._on_revoked is not None:
            self._on_revoked()

    async def _exchange(
        self,
        kind: Command,
        reply: Reply,
        following: Command | None,
        secret: SecretValue | None = None,
        /,
        **fields: str,
    ) -> str:
        self._remaining()
        if self._phase != kind:
            raise BridgeRefused("protocol")
        self._phase = None
        complete = False
        try:
            self._require_binding(False)
            challenge = new_operation_id()
            if secret is not None:
                fields["key"] = secret.reveal()
            await self._send(kind, **fields)
            await self._send("activate", challenge=challenge)
            response = (
                await self._prompt_reply(challenge, self._shell)
                if kind == "prompt" and self._shell is not None
                else self._reply(await self._read(challenge), reply)
            )
            self._phase, complete = following, True
            return response
        finally:
            if not complete:
                self.revoke()

    def _native_cutoff(self) -> int:
        native = (
            time.CLOCK_MONOTONIC_RAW
            if sys.platform == "darwin"
            else time.CLOCK_MONOTONIC
        )
        earlier_native = time.clock_gettime_ns(native)
        return earlier_native + (self._cutoff_ns - self._clock.monotonic_ns()) - 1

    def _remaining(self) -> float:
        if self._revoked:
            raise BridgeRefused("closed")
        remaining = self._cutoff_ns - self._clock.monotonic_ns()
        if remaining <= 0:
            raise BridgeRefused("deadline")
        return remaining / 1_000_000_000

    def _release_waiter(self) -> None:
        waiting, self._waiting = self._waiting, None
        if waiting is not None:
            descriptor, ready = waiting
            try:
                ready.close()
            finally:
                os.close(descriptor)

    async def _wait_writable(self, descriptor: int) -> None:
        duplicate = os.dup(descriptor)
        try:
            self._waiting = (duplicate, DescriptorReadiness(duplicate, writing=True))
        except (OSError, RuntimeError):
            os.close(duplicate)
            raise BridgeRefused("closed") from None
        try:
            await wait_future(self._waiting[1].ready, self._cutoff_ns, self._clock)
        finally:
            self._release_waiter()

    async def _send(
        self,
        kind: str,
        require_current: Callable[[], None] | None = None,
        /,
        **fields: str,
    ) -> None:
        record = (
            json.dumps(
                {"kind": kind, "nonce": self._nonce, **fields},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(record) > MAX_FRAME_BYTES:
            raise BridgeRefused("protocol")
        try:
            remaining = memoryview(record)
            while remaining:
                if require_current is not None:
                    require_current()
                self._require_binding(False)
                self._remaining()
                descriptor = self._endpoint.fileno()
                try:
                    written = os.write(descriptor, remaining)
                except BlockingIOError:
                    await self._wait_writable(descriptor)
                    self._remaining()
                    self._require_binding(False)
                    if require_current is not None:
                        require_current()
                    await self._recheck_installation()
                    continue
                if written <= 0:
                    raise BridgeRefused("closed")
                remaining = remaining[written:]
        except ChannelError:
            raise BridgeRefused("deadline") from None
        except OSError:
            raise BridgeRefused("closed") from None

    async def _read(self, challenge: str | Callable[[], str | None]) -> JsonObject:
        try:
            async with asyncio.timeout(self._remaining()):
                prefix = b""
                if callable(challenge):
                    prefix = await self._reader.readexactly(1)
                    self._remaining()
                    if challenge() is None:
                        raise BridgeRefused("protocol")
                record = (
                    prefix
                    if prefix == b"\n"
                    else prefix + await self._reader.readuntil(b"\n")
                )
        except TimeoutError:
            raise BridgeRefused("closed" if self._revoked else "deadline") from None
        except (OSError, EOFError, asyncio.LimitOverrunError) as error:
            self._remaining()
            oversized = isinstance(error, asyncio.LimitOverrunError)
            raise BridgeRefused("protocol" if oversized else "closed") from None
        self._remaining()
        frame = decode_strict_json(record, MAX_FRAME_BYTES, 1)
        expected = challenge() if callable(challenge) else challenge
        if (
            b"\r" in record
            or not isinstance(frame, dict)
            or frame.pop("nonce", None) != self._nonce
            or expected is None
            or frame.pop("challenge", None) != expected
        ):
            raise BridgeRefused("protocol")
        self._require_binding(frame.get("kind") in ("failure", "terminal"))
        self._remaining()
        return frame

    @staticmethod
    def _reply(frame: JsonObject, expected: Reply) -> str:
        match frame:
            case {"kind": "failure", "code": str(code)} if (
                len(frame) == 2 and code in _FAILURES
            ):
                raise BridgeRefused(code)
            case {"kind": "terminal", "text": str(text)} if (
                len(frame) == 2 and expected == "terminal"
            ):
                return text
            case {"kind": kind} if len(frame) == 1 and kind == expected != "terminal":
                return ""
            case _:
                raise BridgeRefused("protocol")

    async def _prompt_reply(self, challenge: str, shell: PiShellCall) -> str:
        frame = await self._read(challenge)
        match frame:
            case {
                "kind": "bash_call",
                "id": str(id_),
                "call": str(operation),
                "command": str(command),
            } if (
                len(frame) == 4
                and _CALL_ID.fullmatch(id_) is not None
                and _NONCE.fullmatch(operation) is not None
                and operation not in (challenge, self._nonce)
                and 0 < len(command.encode()) <= MAX_COMMAND_BYTES
            ):
                call = BashRequest(id_, operation, command)
            case _:
                if frame.get("kind") == "failure":
                    self._reply(frame, "terminal")
                raise BridgeRefused("protocol")

        released: str | None = None
        peer = asyncio.create_task(self._read(lambda: released))

        def require_current() -> None:
            self._remaining()
            if peer.done():
                peer.result()
                raise BridgeRefused("protocol")
            if self._reader.at_eof():
                raise BridgeRefused("closed")
            self._require_binding(False)

        def current() -> bool:
            try:
                require_current()
            except BridgeRefused:
                return False
            return True

        async def deliver(result: str) -> None:
            nonlocal released
            if len(result.encode()) > MAX_RESULT_BYTES:
                raise BridgeRefused("protocol")
            await self._send(
                "bash_result",
                require_current,
                challenge=challenge,
                call=operation,
                id=id_,
                result=result,
            )
            terminal_challenge = new_operation_id()
            await self._send("activate", require_current, challenge=terminal_challenge)
            released = terminal_challenge

        work = asyncio.create_task(shell.run(call, self._cutoff_ns, current))
        operation_task: asyncio.Task[str] | asyncio.Task[None] = work
        try:
            await asyncio.wait((peer, work), return_when=asyncio.FIRST_COMPLETED)
            require_current()
            delivery = asyncio.create_task(deliver(work.result()))
            operation_task = delivery
            await asyncio.wait((peer, delivery), return_when=asyncio.FIRST_COMPLETED)
            if not delivery.done():
                require_current()
            delivery.result()
            frame = await peer
            self._remaining()
            self._require_binding(frame.get("kind") in ("failure", "terminal"))
            self._remaining()
            return self._reply(frame, "terminal")
        except ShellCallRefused:
            raise BridgeRefused("prompt") from None
        finally:
            if not released or not peer.done():
                self.revoke()
            for pending in (operation_task, peer):
                pending.cancel()
                await observed(cast(asyncio.Future[object], pending))
