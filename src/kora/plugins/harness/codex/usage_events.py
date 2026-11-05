from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import TypeAdapter, ValidationError

from kora.ir.cost import MAX_SUM, MAX_TOKEN, TokenField, UsageCoverage
from kora.ir.json import parse_json_object
from kora.plugins.harness.codex.app_server_usage import ConfirmedUsage
from kora.protocols.harness import EventSink, UsageData
from kora.protocols.identity import Identifier

MESSAGE_ID = TypeAdapter(Identifier)
KNOWN_COUNTS: frozenset[TokenField] = frozenset(
    {"cached_input_tokens", "output_tokens", "reasoning_tokens"}
)
COUNTS_WITH_CACHE_WRITES: frozenset[TokenField] = KNOWN_COUNTS | frozenset[TokenField](
    {"input_tokens", "cache_creation_tokens"}
)


class UsageEventFailure(StrEnum):
    INVALID_OBSERVATION = "codex_usage_event_invalid_observation"
    INVALID_REVISION = "codex_usage_event_invalid_revision"


class UsageEventRefused(ValueError):
    def __init__(self, reason: UsageEventFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class UsageStamp:
    observation_id: str
    sequence: int


class CodexUsageEvents:
    def __init__(
        self,
        sink: EventSink,
        *,
        message_id: str,
        next_stamp: Callable[[], UsageStamp],
    ) -> None:
        try:
            self._message_id = MESSAGE_ID.validate_python(message_id)
        except ValidationError:
            raise UsageEventRefused(UsageEventFailure.INVALID_OBSERVATION) from None
        self._sink = sink
        self._next_stamp = next_stamp
        self._last_stamp: UsageStamp | None = None

    def observe(self, observation: ConfirmedUsage) -> None:
        counts = observation.counts
        inputs = counts.input_tokens
        writes = counts.cache_creation_tokens
        tokens = (
            inputs if inputs is not None else 0,
            counts.cached_input_tokens,
            writes if writes is not None else 0,
            counts.output_tokens,
            counts.reasoning_tokens,
        )
        if (
            any(not 0 <= count <= MAX_TOKEN for count in tokens)
            or observation.observed_at.utcoffset() is None
        ):
            raise UsageEventRefused(UsageEventFailure.INVALID_OBSERVATION)
        stamp = self._revision()
        try:
            usage = UsageData(
                observation_id=stamp.observation_id,
                observation_sequence=stamp.sequence,
                observed_at=observation.observed_at,
                message_id=self._message_id,
                input_tokens=tokens[0],
                cached_input_tokens=tokens[1],
                cache_creation_tokens=tokens[2],
                output_tokens=tokens[3],
                reasoning_tokens=tokens[4],
                known_token_fields=(
                    COUNTS_WITH_CACHE_WRITES if writes is not None else KNOWN_COUNTS
                ),
                basis="price_table",
                billing_class="api_key",
                coverage=UsageCoverage(
                    version=1,
                    observation_id=stamp.observation_id,
                    observation_sequence=stamp.sequence,
                    observed_at=observation.observed_at,
                    completeness="partial",
                    attribution_gap=True,
                    observation_gap=observation.observation_gap,
                ),
            )
        except ValidationError:
            raise UsageEventRefused(UsageEventFailure.INVALID_OBSERVATION) from None
        self._sink.emit("usage", parse_json_object(usage.model_dump(mode="json")))
        self._last_stamp = stamp

    def _revision(self) -> UsageStamp:
        previous = self._last_stamp
        if previous is not None and previous.sequence == MAX_SUM:
            raise UsageEventRefused(UsageEventFailure.INVALID_REVISION)
        current = self._next_stamp()
        if (
            type(current.sequence) is not int
            or not 0 <= current.sequence <= MAX_SUM
            or (
                previous is not None
                and (
                    current.sequence <= previous.sequence
                    or current.observation_id == previous.observation_id
                )
            )
        ):
            raise UsageEventRefused(UsageEventFailure.INVALID_REVISION)
        return current
