from collections.abc import Callable
from json.encoder import encode_basestring

from kora.ir.agent_session_validation import TRUNCATION_MARKER
from kora.ir.json import parse_json_object
from kora.observability.redaction import SecretRegistry, StreamMasker
from kora.plugins.harness.codex.app_server_messages import CompletedMessage
from kora.protocols.harness import MAX_TEXT_BYTES, EventSink, TurnData

MASK_CHUNK_BYTES = 8192
MARKER = TRUNCATION_MARKER.decode("utf-8")


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


class CodexTurnEvents:
    def __init__(
        self,
        sink: EventSink,
        *,
        registry: SecretRegistry,
        check_deadline: Callable[[], None],
    ) -> None:
        self._sink = sink
        self._registry = registry
        self._check_deadline = check_deadline

    def observe(self, ordinal: int, message: CompletedMessage) -> None:
        text, truncated = masked_text(
            message.text, self._registry, self._check_deadline
        )
        data = TurnData(
            message_id=f"codex-message-{ordinal}", text=text, truncated=truncated
        )
        payload = parse_json_object(data.model_dump(mode="json"))
        self._check_deadline()
        self._sink.emit("turn", payload)
