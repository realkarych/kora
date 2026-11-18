from collections.abc import Callable

from kora.ir.json import parse_json_object
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.codex.app_server_messages import CompletedMessage
from kora.plugins.harness.text import masked_text
from kora.protocols.harness import EventSink, TurnData


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
