import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Final, Literal, Protocol, cast

from kora.ir.vcs_ref_json import decode_strict_json
from kora.protocols.lifetime import new_operation_id
from kora.protocols.secret_value import SecretValue
from kora.protocols.time import Clock
from kora.runner.channels import ChannelError, DescriptorReadiness, wait_future
from kora.runner.process_stdio import StdioStreams

MAX_FRAME_BYTES: Final[int] = 65_536
MAX_PROMPT_BYTES: Final[int] = 32_768
_NONCE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_FAILURES: Final[frozenset[str]] = frozenset(
    {"protocol", "startup", "credential", "prompt", "stopped"}
)
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
        require_live: Callable[[], None],
        recheck_installation: Callable[[], Awaitable[None]],
    ) -> None:
        if _NONCE.fullmatch(nonce) is None:
            raise BridgeRefused("protocol")
        self._reader, self._writer = streams
        self._endpoint = cast(_Endpoint, self._writer.get_extra_info("socket"))
        self._nonce = nonce
        self._cutoff_ns = cutoff_ns
        self._clock = clock
        self._require_live = require_live
        self._recheck_installation = recheck_installation
        self._phase: Command | None = "init"
        self._revoked: bool = False
        self._waiting: tuple[int, DescriptorReadiness] | None = None

    async def initialize(self, origin: str, model: str) -> None:
        await self._exchange("init", "ready", "credential", origin=origin, model=model)

    async def credential(self, key: SecretValue) -> None:
        await self._exchange("credential", "credential_ready", "prompt", secret=key)

    async def prompt(self, text: str) -> str:
        if len(text) > MAX_PROMPT_BYTES or len(text.encode()) > MAX_PROMPT_BYTES:
            raise BridgeRefused("protocol")
        return await self._exchange("prompt", "terminal", None, text=text)

    def revoke(self) -> None:
        self._revoked = True
        self._release_waiter()
        self._writer.transport.abort()

    async def _exchange(
        self,
        kind: Command,
        reply: Reply,
        following: Command | None,
        *,
        secret: SecretValue | None = None,
        **fields: str,
    ) -> str:
        self._remaining()
        if self._phase != kind:
            raise BridgeRefused("protocol")
        self._phase = None
        complete = False
        try:
            self._require_live()
            challenge = new_operation_id()
            if secret is not None:
                fields["key"] = secret.reveal()
            await self._send(kind, challenge=challenge, **fields)
            response = await self._receive(reply, challenge)
            self._phase = following
            complete = True
            return response
        finally:
            if not complete:
                self.revoke()

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

    async def _send(self, kind: str, **fields: str) -> None:
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
                self._require_live()
                self._remaining()
                descriptor = self._endpoint.fileno()
                try:
                    written = os.write(descriptor, remaining)
                except BlockingIOError:
                    await self._wait_writable(descriptor)
                    self._remaining()
                    self._require_live()
                    await self._recheck_installation()
                    continue
                if written <= 0:
                    raise BridgeRefused("closed")
                remaining = remaining[written:]
        except ChannelError:
            raise BridgeRefused("deadline") from None
        except OSError:
            raise BridgeRefused("closed") from None

    async def _receive(self, expected: Reply, challenge: str) -> str:
        try:
            async with asyncio.timeout(self._remaining()):
                record = await self._reader.readuntil(b"\n")
        except TimeoutError:
            raise BridgeRefused("deadline") from None
        except (OSError, asyncio.IncompleteReadError):
            raise BridgeRefused("closed") from None
        except asyncio.LimitOverrunError:
            raise BridgeRefused("protocol") from None
        self._remaining()
        frame = decode_strict_json(record, MAX_FRAME_BYTES, 1)
        if (
            b"\r" in record
            or not isinstance(frame, dict)
            or frame.pop("nonce", None) != self._nonce
            or frame.pop("challenge", None) != challenge
        ):
            raise BridgeRefused("protocol")
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
