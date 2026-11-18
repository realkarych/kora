import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from pydantic import ValidationError

from kora.ir import ErrorClass
from kora.ir.cost import (
    MAX_SUM,
    BillingClass,
)
from kora.ir.json import JsonShapeError, parse_json_object
from kora.observability.redaction import SecretRegistry, StreamMasker
from kora.plugins.harness.claude.denials import (
    UNKNOWN_TOOL,
    bounded_identifier,
    harness_denials,
    pre_execution_rejection,
)
from kora.plugins.harness.claude.signals import ProviderSignals
from kora.plugins.harness.claude.stop import (
    CONFIG_ERRORS,
    ClaudeObservation,
    RecordedResult,
    result_class,
)
from kora.plugins.harness.claude.stop import refused as refused
from kora.plugins.harness.claude.usage import ClaudeUsage
from kora.plugins.harness.text import (
    MASK_CHUNK_BYTES,
    clipped_text,
    encoded_text_size,
    masked_text,
)
from kora.protocols.errors import ERROR_CODE
from kora.protocols.harness import (
    MAX_SAMPLE_ITEMS,
    MAX_TEXT_BYTES,
    DenialSample,
    ErrorData,
    EventSink,
    OutputCandidate,
    PermissionDeniedData,
    ProviderLimitedData,
    ProviderRetryingData,
    ResultData,
    SessionStartedData,
    ToolCallData,
    ToolResultData,
    TurnData,
    UsageData,
)
from kora.runner.calls import (
    LIMITED_KINDS,
    MAX_DECIDED,
    TRACKING_CEILING,
    ToolCallLedger,
)


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


def protocol_code(value: object) -> str:
    if not isinstance(value, str) or ERROR_CODE.fullmatch(value) is None:
        refused()
    return value


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


def recorded_result(
    subtype: object, is_error: object, terminal_reason: object
) -> RecordedResult:
    if type(is_error) is not bool:
        refused()
    reason = None if terminal_reason is None else protocol_code(terminal_reason)
    return RecordedResult(protocol_code(subtype), is_error, reason)


class TurnText:
    def __init__(self, registry: SecretRegistry) -> None:
        self.parts: list[str] = []
        self.size = 2
        self.seen = False
        self.pending = bytearray()
        self.masker = StreamMasker(registry, self.collect)

    def collect(self, part: str) -> None:
        if self.size <= MAX_TEXT_BYTES:
            self.parts.append(part)
            self.size += encoded_text_size(part) - 2

    def feed(self, text: str, check_deadline: Callable[[], None]) -> None:
        self.seen = True
        if self.size > MAX_TEXT_BYTES:
            return
        encoded = text.encode("utf-8")
        for start in range(0, len(encoded), MASK_CHUNK_BYTES):
            check_deadline()
            self.pending.extend(encoded[start : start + MASK_CHUNK_BYTES])
            if len(self.pending) >= MASK_CHUNK_BYTES:
                self.masker.feed(bytes(self.pending[:MASK_CHUNK_BYTES]))
                del self.pending[:MASK_CHUNK_BYTES]
            if self.size > MAX_TEXT_BYTES:
                self.pending.clear()
                break

    def finish(self) -> tuple[str | None, bool]:
        if self.pending:
            self.masker.feed(bytes(self.pending))
            self.pending.clear()
        self.masker.close()
        if not self.seen:
            return None, False
        rendered = "".join(self.parts)
        if self.size > MAX_TEXT_BYTES:
            return clipped_text(rendered), True
        return rendered, False


class TextOutcome(StrEnum):
    ABSENT = "absent"
    INVALID = "invalid"
    ACCEPTED = "accepted"


class ClaudeEvents:
    def __init__(
        self,
        sink: EventSink,
        registry: SecretRegistry,
        *,
        binding: str,
        max_retries: int,
        output_format: bool,
        check_deadline: Callable[[], None],
        observed_at: Callable[[], datetime],
    ) -> None:
        self._sink = sink
        self._registry = registry
        self._output_format = output_format
        self._check_deadline = check_deadline
        self._observed_at = observed_at
        self._signals = ProviderSignals(binding, max_retries, observed_at, self.drop)
        self.facts: SessionStartedData | None = None
        self.turns = 0
        self.structured: OutputCandidate | None = None
        self.output_refused = False
        self.latest_usage: UsageData | None = None
        self.result_text: OutputCandidate | None = None
        self.text_outcome = TextOutcome.ABSENT
        self._emitted_calls: set[str] = set()
        self._observed_turns: dict[str, str | None] = {}
        self._started = False
        self._billing: BillingClass | None = None
        self._message_id: str | None = None
        self._agent_id: str | None = None
        self._text = TurnText(registry)
        self._tools: list[ToolPiece] = []
        self._usage = ClaudeUsage(
            self._emit_usage, self._next_usage_sequence, observed_at
        )
        self._usage_sequence = 0
        self._result: RecordedResult | None = None
        self._result_emitted = False
        self._result_errors: tuple[str, ...] = ()
        self._calls = ToolCallLedger()
        self._observed: dict[str, str] = {}
        self._callback_denied: set[str] = set()
        self._assistant_errors: set[str] = set()
        self._provider_limit_seen = False
        self._api_error_status: int | None = None
        self._denials = DenialSample(items=(), total=0)
        self._bypass_denials = self._denials
        self._unmatched_denials = 0

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
        self._signals.model(fields.get("model"))
        self._emit("session_started", started)
        self.facts = started
        self._billing = selected
        self._usage.billing = selected
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
                self._text.feed(piece.text, self._check_deadline)
            else:
                if not bounded_identifier(piece.tool_use_id):
                    refused()
                if (
                    piece.tool_use_id not in self._observed
                    and len(self._observed) >= TRACKING_CEILING
                ):
                    refused()
                if (
                    piece.tool_use_id in self._observed
                    and self._observed[piece.tool_use_id] != piece.name
                ):
                    refused()
                self._observed[piece.tool_use_id] = (
                    piece.name if bounded_identifier(piece.name) else UNKNOWN_TOOL
                )
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
        if tool_use_id in self._callback_denied and not is_error:
            self._calls.confirmed = True
        tool = self._observed.get(tool_use_id, UNKNOWN_TOOL)
        if not pre_execution_rejection(tool, is_error, content):
            self._calls.result(tool, tool_use_id, is_error)
        self._emit("tool_result", result)

    def usage_start(
        self, message_id: str, model: object, agent_id: object, usage: object
    ) -> None:
        if not self._started:
            refused()
        self._usage.start(message_id, model, agent_id, usage)

    def usage_delta(self, usage: object, scope: object = None) -> None:
        if not self._started:
            refused()
        self._usage.delta(usage, scope)

    def usage_stop(self, scope: object = None) -> None:
        if not self._started:
            refused()
        self._usage.stop(scope)

    def observe_result(
        self,
        subtype: object,
        is_error: object,
        terminal_reason: object,
        model_usage: object,
        api_error_status: object = None,
        permission_denials: object = None,
        query_usage: object = None,
        *,
        structured_output: object = None,
        result: object = None,
        errors: object = None,
    ) -> None:
        if not self._started or self._result is not None:
            refused()
        recorded = recorded_result(subtype, is_error, terminal_reason)
        result_errors: list[str] = []
        if errors is not None:
            for error in require_list(errors):
                if not isinstance(error, str):
                    refused()
                result_errors.append(error)
        if api_error_status is not None and (
            type(api_error_status) is not int or not 100 <= api_error_status <= 599
        ):
            refused()
        denials = harness_denials(
            permission_denials, self._observed, self._callback_denied
        )
        self._flush()
        self._usage.result(model_usage, query_usage, recorded.terminal_reason)
        callback_denials = self._denials
        self._denials = DenialSample(
            items=(callback_denials.items + denials.sample.items)[:MAX_SAMPLE_ITEMS],
            total=callback_denials.total + denials.sample.total,
        )
        self._unmatched_denials = denials.unmatched
        held = tuple(
            PermissionDeniedData(
                tool=call.tool,
                tool_use_id=call.tool_use_id,
                source="harness",
                code="harness_rule",
            )
            for call in self._calls.held.values()
            if call.tool_use_id in denials.matched
        )
        self._bypass_denials = DenialSample(items=held, total=len(held))
        self._api_error_status = api_error_status
        self._provider_limit_seen |= api_error_status == 429
        if api_error_status == 429:
            self._emit_rate_signal()
        try:
            self.structured = (
                None
                if structured_output is None
                else OutputCandidate(
                    source="structured_output", value=structured_output
                )
            )
        except ValueError:
            self.structured = None
            self.output_refused = True
        self.result_text = text_candidate(result)
        self.text_outcome = (
            TextOutcome.ACCEPTED
            if self.result_text is not None
            else TextOutcome.INVALID
            if isinstance(result, str)
            else TextOutcome.ABSENT
        )
        self._result = recorded
        self._result_errors = tuple(result_errors)
        self._flush()
        for denial in denials.sample.items:
            self._emit("permission_denied", denial)

    @property
    def calls(self) -> ToolCallLedger:
        return self._calls

    def flush(self) -> None:
        self._flush()

    def call(self, data: ToolCallData) -> None:
        if data.tool_use_id in self._emitted_calls:
            return
        if len(self._emitted_calls) >= MAX_DECIDED:
            refused()
        self._emitted_calls.add(data.tool_use_id)
        self._emit("tool_call", data)

    def deny(self, data: PermissionDeniedData) -> None:
        self._denials = DenialSample(
            items=(*self._denials.items, data)[:MAX_SAMPLE_ITEMS],
            total=self._denials.total + 1,
        )
        self._emit("permission_denied", data)

    def decided(self, tool_use_id: str, allow: bool) -> None:
        if not bounded_identifier(tool_use_id):
            refused()
        if (
            tool_use_id not in self._calls.decided_ids
            and len(self._calls.decided_ids) >= MAX_DECIDED
        ):
            refused()
        self._calls.decided(tool_use_id)
        if not allow:
            self._callback_denied.add(tool_use_id)

    @property
    def bypass_confirmed(self) -> bool:
        return self._calls.bypass(
            self._bypass_denials, self._result is not None
        ).confirmed

    def observe_assistant_error(self, error: object) -> None:
        if error is not None and not isinstance(error, str):
            refused()
        if error in CONFIG_ERRORS:
            self._assistant_errors.add(str(error))
        self._provider_limit_seen |= error == "rate_limit"
        if error == "rate_limit":
            self._emit_rate_signal()

    def observe_model(self, message_id: str | None, model: object) -> None:
        if not self._started:
            return
        new_call = (
            message_id is not None
            and message_id != self._message_id
            and message_id not in self._observed_turns
        )
        self._signals.assistant(new_call, model)

    def _emit_rate_signal(self) -> None:
        data = self._signals.rate()
        if data is not None:
            self._emit("provider_limited", data)

    def observe_retry(self, data: object) -> None:
        signal = self._signals.retry(data)
        if signal is not None:
            self._emit("provider_retrying", signal)

    def observe_rate_limit(
        self, status: object, resets_at: object = None, rate_limit_type: object = None
    ) -> None:
        if status not in ("allowed", "allowed_warning", "rejected"):
            refused()
        self._provider_limit_seen |= status == "rejected"
        if status == "rejected":
            data = self._signals.rejected(resets_at, rate_limit_type)
            if data is not None:
                self._emit("provider_limited", data)

    def observation(self) -> ClaudeObservation:
        recorded = self._result
        seen = recorded is not None
        bypass = self._calls.bypass(self._bypass_denials, seen)
        return ClaudeObservation(
            result_seen=seen,
            result_class=result_class(recorded, self._assistant_errors)
            if recorded
            else ErrorClass.HARNESS_ERROR,
            provider_limit_seen=self._provider_limit_seen,
            terminal_reason=recorded.terminal_reason if recorded else None,
            api_error_status=self._api_error_status,
            denials=self._denials,
            denials_complete=seen
            and self._denials.total == len(self._denials.items)
            and bypass.unconfirmed.total == 0,
            unmatched_denials=self._unmatched_denials,
            bypass=bypass,
            usage=self._usage.summary(),
        )

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

    def finish_session(self) -> None:
        self._flush()
        self._usage.finish()
        self._emit_result()

    def finish(self) -> None:
        self._flush()
        self._usage.finish()
        self._emit_result()
        if not self._started:
            refused()

    def _flush(self) -> None:
        message_id = self._message_id
        if message_id is None:
            return
        text, truncated = self._text.finish()
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
        if message_id not in self._observed_turns:
            if len(self._observed_turns) >= TRACKING_CEILING:
                refused()
            self._observed_turns[message_id] = self._agent_id
            self._emit("turn", turn)
        elif self._observed_turns[message_id] != self._agent_id:
            refused()
        else:
            self._sink.observe_dropped("assistant")
        tools = tuple(self._tools)
        agent_id = self._agent_id
        self._message_id = None
        self._agent_id = None
        self._text = TurnText(self._registry)
        self._tools = []
        for tool in tools:
            self._emit_tool(tool, agent_id)

    def _emit_tool(self, tool: ToolPiece, agent_id: str | None) -> None:
        output_channel = (
            self._output_format and tool.name == "StructuredOutput" and agent_id is None
        )
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
        self.call(call)

    def _next_usage_sequence(self) -> int:
        sequence = self._usage_sequence
        if sequence > MAX_SUM:
            refused()
        self._usage_sequence += 1
        return sequence

    def _emit_usage(self, data: UsageData) -> None:
        self.latest_usage = data
        self._emit("usage", data)

    def _emit_result(self) -> None:
        recorded = self._result
        if recorded is None or self._result_emitted:
            return
        self._result_emitted = True
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
        for error in self._result_errors:
            text, truncated = masked_text(error, self._registry, self._check_deadline)
            self._emit(
                "error",
                ErrorData(code="harness_result_error", text=text, truncated=truncated),
            )
        self._result_errors = ()

    def _emit(
        self,
        kind: str,
        model: SessionStartedData
        | TurnData
        | ToolCallData
        | ToolResultData
        | UsageData
        | ResultData
        | ErrorData
        | PermissionDeniedData
        | ProviderLimitedData
        | ProviderRetryingData,
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
        if kind in LIMITED_KINDS and self._sink.enforcement().cause == "max_events":
            self._sink.observe_dropped(kind)
            return
        self._sink.emit(kind, payload)
        if kind == "turn":
            self.turns += 1


def unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def invalid_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def text_candidate(value: object) -> OutputCandidate | None:
    if not isinstance(value, str):
        return None
    try:
        parsed: object = json.loads(
            value, object_pairs_hook=unique_members, parse_constant=invalid_constant
        )
        return OutputCandidate(source="result_text", value=parsed)
    except (ValueError, RecursionError):
        return None
