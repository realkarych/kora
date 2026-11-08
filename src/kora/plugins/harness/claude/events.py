import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from json.encoder import encode_basestring
from typing import NoReturn, cast

from pydantic import ValidationError

from kora.ir.agent_session_validation import TRUNCATION_MARKER
from kora.ir.cost import BillingClass
from kora.ir.json import JsonShapeError, parse_json_object
from kora.observability.redaction import SecretRegistry, StreamMasker
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    MAX_TEXT_BYTES,
    EventSink,
    SessionStartedData,
    ToolCallData,
    ToolResultData,
    TurnData,
)

MASK_CHUNK_BYTES = 8192
MARKER = TRUNCATION_MARKER.decode("utf-8")


def refused() -> NoReturn:
    raise KoraProtocolError("harness_error", "the Claude session failed", False, {})


def encoded_text_size(text: str) -> int:
    return len(encode_basestring(text).encode("utf-8"))


def clipped_text(text: str) -> str:
    budget = MAX_TEXT_BYTES - encoded_text_size(MARKER) + 2
    lower = 0
    upper = min(len(text), budget - 2)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if encoded_text_size(text[:middle]) <= budget:
            lower = middle
        else:
            upper = middle - 1
    return text[:lower] + MARKER


def masked_text(
    text: str, registry: SecretRegistry, check_deadline: Callable[[], None]
) -> tuple[str, bool]:
    parts: list[str] = []
    size = 2

    def collect(part: str) -> None:
        nonlocal size
        if size <= MAX_TEXT_BYTES:
            parts.append(part)
            size += encoded_text_size(part) - 2

    encoded = text.encode("utf-8")
    with StreamMasker(registry, collect) as masker:
        for start in range(0, len(encoded), MASK_CHUNK_BYTES):
            check_deadline()
            masker.feed(encoded[start : start + MASK_CHUNK_BYTES])
            if size > MAX_TEXT_BYTES:
                break
    rendered = "".join(parts)
    if size > MAX_TEXT_BYTES:
        return clipped_text(rendered), True
    return rendered, False


def billing_class(source: str) -> BillingClass:
    if source == "ANTHROPIC_API_KEY":
        return "api_key"
    if source == "none":
        return "subscription"
    if source == "/login managed key":
        return "managed"
    refused()


def plugins_digest(names: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(names),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        refused()
    return list[object].copy(cast("list[object]", value))


def plugin_names(value: object) -> tuple[str, ...]:
    names: list[str] = []
    for item in require_list(value):
        if not isinstance(item, dict):
            refused()
        name = cast("dict[object, object]", item).get("name")
        if not isinstance(name, str):
            refused()
        names.append(name)
    return tuple(names)


def mcp_sources(value: object) -> tuple[str, ...]:
    sources: list[str] = []
    for item in require_list(value):
        if not isinstance(item, dict):
            refused()
        source = cast("dict[object, object]", item).get("source")
        if source is None:
            continue
        if not isinstance(source, str):
            refused()
        sources.append(source)
    return tuple(sources)


@dataclass(frozen=True)
class TextPiece:
    text: str


@dataclass(frozen=True)
class ToolPiece:
    name: str
    tool_use_id: str


class ClaudeEvents:
    def __init__(
        self,
        sink: EventSink,
        registry: SecretRegistry,
        *,
        output_format: bool,
        check_deadline: Callable[[], None],
    ) -> None:
        self._sink = sink
        self._registry = registry
        self._output_format = output_format
        self._check_deadline = check_deadline
        self._started = False
        self._message_id: str | None = None
        self._agent_id: str | None = None
        self._parts: list[str] = []
        self._saw_text = False
        self._tools: list[ToolPiece] = []

    def observe_init(self, data: object) -> None:
        if self._started or not isinstance(data, dict):
            refused()
        fields = cast(dict[object, object], data)
        session_id = fields.get("session_id")
        version = fields.get("claude_code_version")
        source = fields.get("apiKeySource")
        if (
            not isinstance(session_id, str)
            or not isinstance(version, str)
            or not isinstance(source, str)
            or "plugins" not in fields
            or "mcp_servers" not in fields
        ):
            refused()
        names = plugin_names(fields.get("plugins"))
        sources = mcp_sources(fields.get("mcp_servers"))
        selected = billing_class(source)
        try:
            started = SessionStartedData(
                session_id=session_id,
                harness_version=version,
                billing_class=selected,
                plugins_digest=plugins_digest(names),
                mcp_sources=sources,
            )
        except ValidationError:
            started = None
        if started is None:
            refused()
        self._emit("session_started", started)
        self._started = True

    def assistant(
        self,
        message_id: str,
        agent_id: str | None,
        pieces: tuple[TextPiece | ToolPiece, ...],
    ) -> None:
        if not self._started:
            refused()
        if self._message_id == message_id:
            if agent_id != self._agent_id:
                refused()
        else:
            self._flush()
            self._message_id = message_id
            self._agent_id = agent_id
        for piece in pieces:
            if isinstance(piece, TextPiece):
                self._parts.append(piece.text)
                self._saw_text = True
            else:
                self._tools.append(piece)

    def tool_result(
        self, tool_use_id: str, is_error: bool, content: str | None
    ) -> None:
        if not self._started:
            refused()
        self._flush()
        text: str | None = None
        truncated = False
        if content is not None:
            text, truncated = masked_text(content, self._registry, self._check_deadline)
        try:
            result = ToolResultData(
                tool_use_id=tool_use_id,
                is_error=is_error,
                content=text,
                truncated=truncated,
            )
        except ValidationError:
            result = None
        if result is None:
            refused()
        self._emit("tool_result", result)

    def drop(self, kind: str) -> None:
        known = {
            "assistant",
            "message",
            "result",
            "stream_event",
            "system",
            "user",
        }
        self._sink.observe_dropped(kind if kind in known else "message")

    def finish(self) -> None:
        self._flush()
        if not self._started:
            refused()

    def _flush(self) -> None:
        message_id = self._message_id
        if message_id is None:
            return
        text: str | None = None
        truncated = False
        if self._saw_text:
            text, truncated = masked_text(
                "".join(self._parts), self._registry, self._check_deadline
            )
        try:
            turn = TurnData(
                message_id=message_id,
                agent_id=self._agent_id,
                text=text,
                truncated=truncated,
            )
        except ValidationError:
            turn = None
        if turn is None:
            refused()
        self._emit("turn", turn)
        tools = tuple(self._tools)
        agent_id = self._agent_id
        self._message_id = None
        self._agent_id = None
        self._parts = []
        self._saw_text = False
        self._tools = []
        for tool in tools:
            self._emit_tool(tool, agent_id)

    def _emit_tool(self, tool: ToolPiece, agent_id: str | None) -> None:
        output_channel = self._output_format and tool.name == "StructuredOutput"
        try:
            call = ToolCallData(
                tool=tool.name,
                tool_use_id=tool.tool_use_id,
                agent_id=agent_id,
                output_channel=output_channel,
            )
        except ValidationError:
            call = None
        if call is None:
            refused()
        self._emit("tool_call", call)

    def _emit(
        self,
        kind: str,
        model: SessionStartedData | TurnData | ToolCallData | ToolResultData,
    ) -> None:
        try:
            dumped = model.model_dump(mode="json")
        except ValidationError:
            dumped = None
        if not isinstance(dumped, dict):
            refused()
        try:
            payload = parse_json_object(dumped)
        except JsonShapeError:
            payload = None
        if payload is None:
            refused()
        self._sink.emit(kind, payload)
