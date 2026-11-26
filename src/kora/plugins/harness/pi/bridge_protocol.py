import asyncio
import json
import re
from typing import Final, Literal

from kora.ir.vcs_ref_json import decode_strict_json
from kora.protocols.secret_value import SecretValue
from kora.protocols.time import Clock
from kora.runner.process_stdio import StdioStreams

MAX_FRAME_BYTES: Final[int] = 65_536
MAX_PROMPT_BYTES: Final[int] = 32_768
_NONCE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_FAILURES: Final[frozenset[str]] = frozenset(
    {"protocol", "startup", "credential", "prompt", "stopped"}
)


class BridgeRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("pi_bridge_refused")


class BridgeChannel:
    def __init__(
        self, streams: StdioStreams, nonce: str, cutoff_ns: int, clock: Clock
    ) -> None:
        if _NONCE.fullmatch(nonce) is None:
            raise BridgeRefused("protocol")
        self._reader, self._writer = streams
        self._writer.transport.set_write_buffer_limits(high=MAX_FRAME_BYTES, low=0)
        self._nonce = nonce
        self._cutoff_ns = cutoff_ns
        self._clock = clock

    async def initialize(self, origin: str, model: str) -> None:
        await self._send("init", origin=origin, model=model)
        await self._receive("ready")

    async def credential(self, key: SecretValue) -> None:
        self._remaining()
        await self._send("credential", key=key.reveal())
        await self._receive("credential_ready")

    async def prompt(self, text: str) -> str:
        if len(text) > MAX_PROMPT_BYTES or len(text.encode()) > MAX_PROMPT_BYTES:
            raise BridgeRefused("protocol")
        await self._send("prompt", text=text)
        return await self._receive("terminal")

    def _remaining(self) -> float:
        remaining = self._cutoff_ns - self._clock.monotonic_ns()
        if remaining <= 0:
            raise BridgeRefused("deadline")
        if self._writer.is_closing():
            raise BridgeRefused("closed")
        return remaining / 1_000_000_000

    async def _send(self, kind: str, **fields: str) -> None:
        record = (
            json.dumps(
                {"kind": kind, "nonce": self._nonce, **fields},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if (
            len(record) > MAX_FRAME_BYTES
            or self._writer.transport.get_write_buffer_size()
        ):
            raise BridgeRefused("protocol")
        try:
            async with asyncio.timeout(self._remaining()):
                self._writer.write(record)
                await self._writer.drain()
                self._remaining()
        except TimeoutError:
            raise BridgeRefused("deadline") from None
        except OSError:
            raise BridgeRefused("closed") from None

    async def _receive(
        self, expected: Literal["ready", "credential_ready", "terminal"]
    ) -> str:
        try:
            async with asyncio.timeout(self._remaining()):
                record = await self._reader.readuntil(b"\n")
        except TimeoutError:
            raise BridgeRefused("deadline") from None
        except (OSError, asyncio.IncompleteReadError):
            raise BridgeRefused("closed") from None
        except asyncio.LimitOverrunError:
            raise BridgeRefused("protocol") from None
        if self._clock.monotonic_ns() >= self._cutoff_ns:
            raise BridgeRefused("deadline")
        if b"\r" in record:
            raise BridgeRefused("protocol")
        frame = decode_strict_json(record, MAX_FRAME_BYTES, 1)
        if not isinstance(frame, dict) or frame.get("nonce") != self._nonce:
            raise BridgeRefused("protocol")
        kind, code, text = frame.get("kind"), frame.get("code"), frame.get("text")
        if (
            kind == "failure"
            and frame.keys() == {"kind", "nonce", "code"}
            and isinstance(code, str)
            and code in _FAILURES
        ):
            raise BridgeRefused(code)
        if kind == expected:
            if (
                expected == "terminal"
                and frame.keys() == {"kind", "nonce", "text"}
                and isinstance(text, str)
            ):
                return text
            if expected != "terminal" and frame.keys() == {"kind", "nonce"}:
                return ""
        raise BridgeRefused("protocol")
