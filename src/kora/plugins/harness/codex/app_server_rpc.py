import asyncio
import json
import math
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

READ_SIZE = 8192
MAX_ENCODING_DEPTH = 64
MAX_ENCODING_INTEGER_BITS = 1024
MAX_DECODING_INTEGER_DIGITS = 1024
MIN_RPC_INTEGER: int = -(1 << 63)
MAX_RPC_INTEGER: int = (1 << 63) - 1


class RpcFailure(StrEnum):
    BUSY = "rpc_busy"
    UNUSABLE = "rpc_unusable"
    DEADLINE = "rpc_deadline"
    DISCONNECTED = "rpc_disconnected"
    TRUNCATED = "rpc_truncated"
    FRAME_LIMIT = "rpc_frame_limit"
    MESSAGE_LIMIT = "rpc_message_limit"
    INVALID_FRAME = "rpc_invalid_frame"
    INVALID_OUTBOUND = "rpc_invalid_outbound"
    UNEXPECTED_REPLY = "rpc_unexpected_reply"
    SERVER_REQUEST = "rpc_server_request"
    REMOTE_ERROR = "rpc_remote_error"


class RpcError(Exception):
    def __init__(self, reason: RpcFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class Notification:
    method: str
    params: object
    emitted_at_ms: int | None


@dataclass(frozen=True)
class Reply:
    request_id: int
    result: object
    refused: bool


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def finite_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return value


def bounded_integer(text: str) -> int:
    if len(text) > MAX_DECODING_INTEGER_DIGITS:
        raise ValueError
    return int(text)


def decode_object(frame: bytes) -> dict[str, object] | None:
    try:
        value: object = json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_float=finite_number,
            parse_int=bounded_integer,
            parse_constant=finite_number,
        )
    except (ValueError, RecursionError):
        return None
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def rpc_integer(value: object) -> bool:
    return type(value) is int and MIN_RPC_INTEGER <= value <= MAX_RPC_INTEGER


def remote_error(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = cast("dict[str, object]", value)
    return (
        fields.keys() <= {"code", "message", "data"}
        and rpc_integer(fields.get("code"))
        and isinstance(fields.get("message"), str)
    )


def decode_message(frame: bytes) -> Notification | Reply:
    fields = decode_object(frame)
    if fields is None or fields.get("jsonrpc", "2.0") != "2.0":
        raise RpcError(RpcFailure.INVALID_FRAME)
    method = fields.get("method")
    if isinstance(method, str) and method:
        if "id" in fields:
            raise RpcError(RpcFailure.SERVER_REQUEST)
        emitted_at_ms = fields.get("emittedAtMs")
        if fields.keys() <= {"jsonrpc", "method", "params", "emittedAtMs"} and (
            emitted_at_ms is None or rpc_integer(emitted_at_ms)
        ):
            return Notification(
                method, fields.get("params"), cast("int | None", emitted_at_ms)
            )
    request_id = fields.get("id")
    if rpc_integer(request_id):
        request_id = cast("int", request_id)
        if "result" in fields and fields.keys() <= {"jsonrpc", "id", "result"}:
            return Reply(request_id, fields["result"], refused=False)
        if fields.keys() <= {"jsonrpc", "id", "error"} and remote_error(
            fields.get("error")
        ):
            return Reply(request_id, None, refused=True)
    raise RpcError(RpcFailure.INVALID_FRAME)


def admit_encoding(value: object, remaining: int, depth: int) -> int:
    if remaining < 1:
        raise RpcError(RpcFailure.FRAME_LIMIT)
    if depth > MAX_ENCODING_DEPTH:
        raise RpcError(RpcFailure.INVALID_OUTBOUND)
    if value is None or value is True:
        size = 4
    elif value is False:
        size = 5
    elif type(value) is int:
        if value.bit_length() > MAX_ENCODING_INTEGER_BITS:
            raise RpcError(RpcFailure.INVALID_OUTBOUND)
        size = len(str(value))
    elif type(value) is float and math.isfinite(value):
        size = len(repr(value))
    elif type(value) is str:
        size = len(value) + 2
    elif isinstance(value, list):
        items = cast("list[object]", value)
        remaining -= 2 + max(len(items) - 1, 0)
        for item in items:
            remaining = admit_encoding(item, remaining, depth + 1)
        size = 0
    elif isinstance(value, dict):
        entries = cast("dict[object, object]", value)
        remaining -= 2 + len(entries) + max(len(entries) - 1, 0)
        for key, item in entries.items():
            if type(key) is not str:
                raise RpcError(RpcFailure.INVALID_OUTBOUND)
            remaining = admit_encoding(key, remaining, depth + 1)
            remaining = admit_encoding(item, remaining, depth + 1)
        size = 0
    else:
        raise RpcError(RpcFailure.INVALID_OUTBOUND)
    if size > remaining:
        raise RpcError(RpcFailure.FRAME_LIMIT)
    return remaining - size


def encode_frame(message: dict[str, object], limit: int) -> bytes:
    admit_encoding(message, limit, 0)
    try:
        payload = json.dumps(
            message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, RecursionError):
        payload = b""
    if not payload:
        raise RpcError(RpcFailure.INVALID_OUTBOUND)
    if len(payload) > limit:
        raise RpcError(RpcFailure.FRAME_LIMIT)
    return payload + b"\n"


class AppServerRpc:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        clock: Callable[[], float],
        max_frame_bytes: int,
        max_messages: int,
    ) -> None:
        if max_frame_bytes < 1 or max_messages < 1:
            raise ValueError("invalid app-server RPC budget")
        self._clock = clock
        self._reader = reader
        self._writer = writer
        self._max_frame_bytes = max_frame_bytes
        self._remaining_messages = max_messages
        self._lookahead = b""
        self._next_id = 1
        self._busy = False
        self._unusable = False

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise RpcError(RpcFailure.DEADLINE)

    @asynccontextmanager
    async def _operation(self, deadline: float) -> AsyncGenerator[None]:
        if self._unusable:
            raise RpcError(RpcFailure.UNUSABLE)
        if self._busy:
            raise RpcError(RpcFailure.BUSY)
        self._busy = True
        completed = False
        try:
            async with asyncio.timeout_at(deadline):
                self._check_deadline(deadline)
                yield
                self._check_deadline(deadline)
            completed = True
        except TimeoutError:
            reason = RpcFailure.DEADLINE
        except OSError:
            reason = RpcFailure.DISCONNECTED
        else:
            return
        finally:
            self._busy = False
            if not completed:
                self._unusable = True
                self._lookahead = b""
        raise RpcError(reason)

    async def _read_line(self, deadline: float) -> bytes:
        frame = bytearray()
        chunk = self._lookahead
        self._lookahead = b""
        while True:
            self._check_deadline(deadline)
            if not chunk:
                allowance = self._max_frame_bytes - len(frame) + 1
                chunk = await self._reader.read(min(READ_SIZE, allowance))
                if not chunk:
                    raise RpcError(
                        RpcFailure.TRUNCATED if frame else RpcFailure.DISCONNECTED
                    )
            newline = chunk.find(b"\n")
            if newline >= 0:
                self._lookahead = chunk[newline + 1 :]
                frame.extend(chunk[:newline])
                return bytes(frame)
            frame.extend(chunk)
            if len(frame) > self._max_frame_bytes:
                raise RpcError(RpcFailure.FRAME_LIMIT)
            chunk = b""

    async def _receive(self, deadline: float) -> Notification | Reply:
        if not self._remaining_messages:
            raise RpcError(RpcFailure.MESSAGE_LIMIT)
        frame = await self._read_line(deadline)
        self._check_deadline(deadline)
        message = decode_message(frame)
        self._check_deadline(deadline)
        self._remaining_messages -= 1
        return message

    async def _send(self, message: dict[str, object], deadline: float) -> None:
        frame = encode_frame(message, self._max_frame_bytes)
        self._check_deadline(deadline)
        self._writer.write(frame)
        await self._writer.drain()

    async def call(
        self,
        method: str,
        params: dict[str, object],
        *,
        deadline: float,
        observe: Callable[[Notification], None],
    ) -> object:
        async with self._operation(deadline):
            request_id = self._next_id
            if request_id > MAX_RPC_INTEGER:
                raise RpcError(RpcFailure.UNUSABLE)
            self._next_id += 1
            await self._send(
                {"id": request_id, "method": method, "params": params}, deadline
            )
            while isinstance(message := await self._receive(deadline), Notification):
                observe(message)
            if message.request_id != request_id:
                raise RpcError(RpcFailure.UNEXPECTED_REPLY)
        if message.refused:
            raise RpcError(RpcFailure.REMOTE_ERROR)
        return message.result

    async def notify(self, method: str, *, deadline: float) -> None:
        async with self._operation(deadline):
            await self._send({"method": method}, deadline)

    async def receive(self, *, deadline: float) -> Notification:
        async with self._operation(deadline):
            message = await self._receive(deadline)
            if isinstance(message, Reply):
                raise RpcError(RpcFailure.UNEXPECTED_REPLY)
        return message
