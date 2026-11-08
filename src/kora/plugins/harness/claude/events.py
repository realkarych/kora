import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from json.encoder import encode_basestring
from typing import NoReturn, cast

from pydantic import ValidationError

from kora.ir.agent_session_validation import TRUNCATION_MARKER
from kora.ir.cost import (
    MAX_IDENTIFIER_LENGTH,
    MAX_SUM,
    MAX_TOKEN,
    BillingClass,
    TokenField,
    UsageCoverage,
)
from kora.ir.json import JsonShapeError, parse_json_object
from kora.observability.redaction import SecretRegistry, StreamMasker
from kora.protocols.errors import ERROR_CODE, KoraProtocolError
from kora.protocols.harness import (
    MAX_TEXT_BYTES,
    EventSink,
    ResultData,
    SessionStartedData,
    ToolCallData,
    ToolResultData,
    TurnData,
    UsageData,
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


START_COUNTS: tuple[tuple[str, TokenField], ...] = (
    ("input_tokens", "input_tokens"),
    ("cache_read_input_tokens", "cached_input_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
)
DELTA_COUNTS: tuple[tuple[str, TokenField], ...] = (
    ("output_tokens", "output_tokens"),
    ("reasoning_tokens", "reasoning_tokens"),
)
OUTPUT_FIELDS: frozenset[TokenField] = frozenset(("output_tokens", "reasoning_tokens"))
MODEL_COUNTS: tuple[tuple[str, TokenField], ...] = (
    ("inputTokens", "input_tokens"),
    ("cacheReadInputTokens", "cached_input_tokens"),
    ("cacheCreationInputTokens", "cache_creation_tokens"),
    ("outputTokens", "output_tokens"),
)
FINAL_FIELDS: frozenset[TokenField] = frozenset(
    (
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_tokens",
        "output_tokens",
    )
)


def protocol_code(value: object) -> str:
    if not isinstance(value, str) or ERROR_CODE.fullmatch(value) is None:
        refused()
    return value


def require_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDENTIFIER_LENGTH
        or not value.isprintable()
    ):
        refused()
    return value


def optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    return require_identifier(value)


def token_count(value: object) -> int:
    if type(value) is not int or value < 0 or value > MAX_TOKEN:
        refused()
    return value


def usage_counts(
    value: object, names: tuple[tuple[str, TokenField], ...]
) -> tuple[dict[TokenField, int], frozenset[TokenField]]:
    if not isinstance(value, dict):
        refused()
    fields = cast("dict[object, object]", value)
    counts: dict[TokenField, int] = {}
    known: set[TokenField] = set()
    for source, target in names:
        if source not in fields:
            counts[target] = 0
            continue
        counts[target] = token_count(fields[source])
        known.add(target)
    return counts, frozenset(known)


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


@dataclass(frozen=True)
class RecordedResult:
    subtype: str
    is_error: bool
    terminal_reason: str | None


def recorded_result(
    subtype: object, is_error: object, terminal_reason: object
) -> RecordedResult:
    if type(is_error) is not bool:
        refused()
    reason = None if terminal_reason is None else protocol_code(terminal_reason)
    return RecordedResult(protocol_code(subtype), is_error, reason)


@dataclass
class OpenUsage:
    message_id: str
    model: str | None
    agent_id: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_creation_tokens: int
    output_tokens: int
    reasoning_tokens: int
    known: frozenset[TokenField]
    complete: bool = False


def apply_final_usage(call: OpenUsage | None, model_usage: object) -> None:
    if model_usage is None:
        return
    if not isinstance(model_usage, dict):
        refused()
    entries = cast("dict[object, object]", model_usage)
    if not entries:
        return
    if call is None or len(entries) != 1:
        refused()
    key, entry = next(iter(entries.items()))
    model = require_identifier(key)
    if call.model is not None and call.model != model:
        refused()
    counts, known = usage_counts(entry, MODEL_COUNTS)
    if known != FINAL_FIELDS:
        refused()
    output = counts["output_tokens"]
    keep_reasoning = (
        "reasoning_tokens" in call.known and call.reasoning_tokens <= output
    )
    call.model = model
    call.input_tokens = counts["input_tokens"]
    call.cached_input_tokens = counts["cached_input_tokens"]
    call.cache_creation_tokens = counts["cache_creation_tokens"]
    call.output_tokens = output
    if not keep_reasoning:
        call.reasoning_tokens = 0
    call.known = FINAL_FIELDS | (
        frozenset(("reasoning_tokens",)) if keep_reasoning else frozenset()
    )
    call.complete = True


class ClaudeEvents:
    def __init__(
        self,
        sink: EventSink,
        registry: SecretRegistry,
        *,
        output_format: bool,
        check_deadline: Callable[[], None],
        observed_at: Callable[[], datetime],
    ) -> None:
        self._sink = sink
        self._registry = registry
        self._output_format = output_format
        self._check_deadline = check_deadline
        self._observed_at = observed_at
        self._started = False
        self._billing: BillingClass | None = None
        self._message_id: str | None = None
        self._agent_id: str | None = None
        self._parts: list[str] = []
        self._saw_text = False
        self._tools: list[ToolPiece] = []
        self._usage: OpenUsage | None = None
        self._usage_sequence = 0
        self._result: RecordedResult | None = None

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
        self._billing = selected
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

    def usage_start(
        self, message_id: str, model: object, agent_id: object, usage: object
    ) -> None:
        if not self._started:
            refused()
        identity = require_identifier(message_id)
        if self._usage is not None and self._usage.message_id == identity:
            refused()
        self._emit_usage()
        counts, known = usage_counts(usage, START_COUNTS)
        self._usage = OpenUsage(
            message_id=identity,
            model=optional_identifier(model),
            agent_id=optional_identifier(agent_id),
            input_tokens=counts["input_tokens"],
            cached_input_tokens=counts["cached_input_tokens"],
            cache_creation_tokens=counts["cache_creation_tokens"],
            output_tokens=0,
            reasoning_tokens=0,
            known=known,
        )

    def usage_delta(self, usage: object) -> None:
        call = self._usage
        if not self._started or call is None:
            refused()
        counts, known = usage_counts(usage, DELTA_COUNTS)
        call.output_tokens = counts["output_tokens"]
        call.reasoning_tokens = counts["reasoning_tokens"]
        call.known = call.known - OUTPUT_FIELDS | known

    def observe_result(
        self,
        subtype: object,
        is_error: object,
        terminal_reason: object,
        model_usage: object,
    ) -> None:
        if not self._started or self._result is not None:
            refused()
        recorded = recorded_result(subtype, is_error, terminal_reason)
        apply_final_usage(self._usage, model_usage)
        self._result = recorded

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
        self._emit_usage()
        self._emit_result()
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

    def _emit_usage(self) -> None:
        call = self._usage
        if call is None:
            return
        self._usage = None
        billing = self._billing
        sequence = self._usage_sequence
        if billing is None or sequence > MAX_SUM:
            refused()
        when = self._observed_at()
        try:
            usage = UsageData(
                observation_id=call.message_id,
                observation_sequence=sequence,
                observed_at=when,
                message_id=call.message_id,
                agent_id=call.agent_id,
                model=call.model,
                input_tokens=call.input_tokens,
                cached_input_tokens=call.cached_input_tokens,
                cache_creation_tokens=call.cache_creation_tokens,
                output_tokens=call.output_tokens,
                reasoning_tokens=call.reasoning_tokens,
                known_token_fields=call.known,
                basis="price_table",
                billing_class=billing,
                coverage=UsageCoverage(
                    version=1,
                    observation_id=call.message_id,
                    observation_sequence=sequence,
                    completeness="complete" if call.complete else "partial",
                    observed_at=when,
                    attribution_gap=call.model is None,
                    observation_gap=False,
                ),
            )
        except ValidationError:
            usage = None
        if usage is None:
            refused()
        self._emit("usage", usage)
        self._usage_sequence = sequence + 1

    def _emit_result(self) -> None:
        recorded = self._result
        if recorded is None:
            return
        self._result = None
        try:
            result = ResultData(
                subtype=recorded.subtype,
                is_error=recorded.is_error,
                terminal_reason=recorded.terminal_reason,
            )
        except ValidationError:
            result = None
        if result is None:
            refused()
        self._emit("result", result)

    def _emit(
        self,
        kind: str,
        model: SessionStartedData
        | TurnData
        | ToolCallData
        | ToolResultData
        | UsageData
        | ResultData,
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
