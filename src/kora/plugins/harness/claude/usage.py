from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from pydantic import ValidationError

from kora.ir.cost import (
    MAX_IDENTIFIER_LENGTH,
    MAX_TOKEN,
    BillingClass,
    Completeness,
    MoneyOverflow,
    TokenField,
    UsageCoverage,
    UsageRow,
    quantize_money,
)
from kora.plugins.harness.claude.stop import UsageSummary, refused
from kora.protocols.harness import UsageData
from kora.runner.usage import (
    MAX_CLOSED_MESSAGES,
    MAX_MODELS,
    MAX_OPEN_MESSAGES,
    UsageLedger,
)

START_COUNTS: tuple[tuple[str, TokenField], ...] = (
    ("input_tokens", "input_tokens"),
    ("cache_read_input_tokens", "cached_input_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
)
DELTA_COUNTS: tuple[tuple[str, TokenField], ...] = (
    ("output_tokens", "output_tokens"),
    ("reasoning_tokens", "reasoning_tokens"),
)
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


@dataclass
class Call:
    message_id: str
    model: str | None
    agent_id: str | None
    start: dict[TokenField, int]
    start_known: frozenset[TokenField]
    delta: dict[TokenField, int]
    delta_known: frozenset[TokenField]

    def same_start(self, other: "Call") -> bool:
        return (self.model, self.agent_id, self.start, self.start_known) == (
            other.model,
            other.agent_id,
            other.start,
            other.start_known,
        )


def native_money(value: object) -> str:
    if type(value) is not int and type(value) is not float:
        refused()
    amount = Decimal(value) if isinstance(value, int) else Decimal(repr(value))
    try:
        result = quantize_money(amount)
    except MoneyOverflow:
        result = None
    if result is None:
        refused()
    return result


def row_counts(row: UsageRow) -> dict[TokenField, int]:
    return {
        "input_tokens": row.input_tokens,
        "cached_input_tokens": row.cached_input_tokens,
        "cache_creation_tokens": row.cache_creation_tokens,
        "output_tokens": row.output_tokens,
        "reasoning_tokens": row.reasoning_tokens,
    }


def revised(row: UsageRow, changes: dict[str, object]) -> UsageRow:
    return UsageRow.model_validate(row.model_dump() | changes)


class ClaudeUsage:
    def __init__(
        self,
        emit: Callable[[UsageData], None],
        next_sequence: Callable[[], int],
        observed_at: Callable[[], datetime],
    ) -> None:
        self._emit = emit
        self._next_sequence = next_sequence
        self._clock = observed_at
        self.billing: BillingClass | None = None
        self.open: dict[str | None, Call] = {}
        self.closed: OrderedDict[str, Call] = OrderedDict()
        self.stopped: OrderedDict[str | None, Call] = OrderedDict()
        self.ledger = UsageLedger()
        self.tail = UsageLedger()
        self.final: dict[str, UsageRow] = {}
        self.models: set[str] = set()
        self.result_seen = False
        self.aborted = False
        self.subagent = False
        self.dedupe_lost = False
        self.observation_gap = False
        self.attribution_gap = False
        self.observed_at: datetime | None = None

    def start(
        self, message_id: str, model: object, agent_id: object, usage: object
    ) -> None:
        counts, known = usage_counts(usage, START_COUNTS)
        call = Call(
            require_identifier(message_id),
            optional_identifier(model),
            optional_identifier(agent_id),
            counts,
            known,
            {},
            frozenset(),
        )
        previous = self.closed.get(call.message_id)
        if previous is None:
            previous = next(
                (
                    item
                    for item in self.open.values()
                    if item.message_id == call.message_id
                ),
                None,
            )
        if previous is not None:
            self.observation_gap |= not previous.same_start(call)
            return
        self.subagent |= call.agent_id is not None
        self.stop(call.agent_id)
        if self.dedupe_lost or len(self.open) >= MAX_OPEN_MESSAGES:
            self.observation_gap = True
            return
        self.open[call.agent_id] = call

    def delta(self, usage: object, scope: object = None) -> None:
        counts, known = usage_counts(usage, DELTA_COUNTS)
        identity = optional_identifier(scope)
        call = self.open.get(identity)
        if call is None:
            last = self.stopped.get(identity)
            redelivered = (
                last is not None and last.delta == counts and last.delta_known == known
            )
            self.observation_gap |= not redelivered
            return
        call.delta = counts
        call.delta_known = known

    def stop(self, scope: object = None) -> None:
        call = self.open.get(optional_identifier(scope))
        if call is None:
            return
        billing = self.billing
        if billing is None:
            refused()
        sequence = self._next_sequence()
        when = self._clock()
        try:
            data = UsageData(
                observation_id=call.message_id,
                observation_sequence=sequence,
                observed_at=when,
                message_id=call.message_id,
                agent_id=call.agent_id,
                model=call.model,
                input_tokens=call.start["input_tokens"],
                cached_input_tokens=call.start["cached_input_tokens"],
                cache_creation_tokens=call.start["cache_creation_tokens"],
                output_tokens=call.delta.get("output_tokens", 0),
                reasoning_tokens=call.delta.get("reasoning_tokens", 0),
                known_token_fields=call.start_known | call.delta_known,
                basis="price_table",
                billing_class=billing,
                coverage=UsageCoverage(
                    version=1,
                    observation_id=call.message_id,
                    observation_sequence=sequence,
                    observed_at=when,
                    completeness="partial",
                    attribution_gap=call.model is None,
                    observation_gap=self.observation_gap,
                ),
            )
        except ValidationError:
            data = None
        if data is None:
            refused()
        self._emit(data)
        del self.open[call.agent_id]
        ledger = self.tail if self.result_seen else self.ledger
        if self.admit_model(call.model):
            ledger.observe(data)
            ledger.close(call.message_id)
        self.closed[call.message_id] = call
        self.stopped[call.agent_id] = call
        self.stopped.move_to_end(call.agent_id)
        if len(self.stopped) > MAX_OPEN_MESSAGES:
            self.stopped.popitem(last=False)
        if len(self.closed) > MAX_CLOSED_MESSAGES:
            self.closed.popitem(last=False)
            self.dedupe_lost = True
            self.observation_gap = True
        self.observed_at = when

    def admit_model(self, model: str | None) -> bool:
        if model is None or model not in self.models and len(self.models) >= MAX_MODELS:
            self.attribution_gap = True
            return False
        self.models.add(model)
        return True

    def finish(self) -> None:
        for scope in tuple(self.open):
            self.stop(scope)

    def result(
        self, model_usage: object, query_usage: object, terminal_reason: str | None
    ) -> None:
        self.finish()
        if model_usage is not None and not isinstance(model_usage, dict):
            refused()
        entries = (
            {} if model_usage is None else cast("dict[object, object]", model_usage)
        )
        when = self._clock()
        prepared: dict[str, UsageRow] = {}
        models = set(self.models)
        attribution_gap = self.attribution_gap
        for key, entry in entries.items():
            model = require_identifier(key)
            try:
                row = self.final_row(model, entry, when)
                if len(entries) == 1 and not self.subagent:
                    row = self.with_ttl(row, query_usage)
            except ValidationError:
                row = None
            if row is None:
                refused()
            if model not in models and len(models) >= MAX_MODELS:
                attribution_gap = True
            else:
                models.add(model)
                prepared[model] = row
        self.final = {
            model: revised(row, {"observation_sequence": self._next_sequence()})
            for model, row in prepared.items()
        }
        self.models = models
        self.attribution_gap = attribution_gap
        if entries:
            self.observed_at = when
            self.compare_calls()
        self.result_seen = True
        self.aborted = terminal_reason == "aborted_streaming"

    def final_row(self, model: str, entry: object, when: datetime) -> UsageRow:
        counts, known = usage_counts(entry, MODEL_COUNTS)
        fields = cast("dict[object, object]", entry)
        amount = native_money(fields["costUSD"]) if "costUSD" in fields else None
        listed = fields.get("costBasis") == "list" and self.billing != "subscription"
        if self.billing is None:
            refused()
        return UsageRow(
            model=model,
            input_tokens=counts["input_tokens"],
            cached_input_tokens=counts["cached_input_tokens"],
            cache_creation_tokens=counts["cache_creation_tokens"],
            output_tokens=counts["output_tokens"],
            reasoning_tokens=0,
            known_token_fields=known,
            cache_creation_by_ttl=None,
            reported_usd=amount if listed else None,
            basis="harness_list" if listed else "price_table",
            billing_class=self.billing,
            observation_id=require_identifier(f"model-usage:{model}"),
            observation_sequence=0,
            observed_at=when,
            completeness="complete",
            delta_of_session_totals=False,
            source=cast("dict[str, object]", fields),
        )

    def with_ttl(self, row: UsageRow, query: object) -> UsageRow:
        if not isinstance(query, dict):
            return row
        fields = cast("dict[object, object]", query)
        split = fields.get("cache_creation")
        if split is None:
            return row
        counts, known = usage_counts(fields, START_COUNTS + DELTA_COUNTS[:1])
        if (
            known != FINAL_FIELDS
            or row.known_token_fields != FINAL_FIELDS
            or any(counts[field] != row_counts(row)[field] for field in FINAL_FIELDS)
        ):
            return row
        if not isinstance(split, dict):
            refused()
        values = cast("dict[object, object]", split)
        ttl = {
            "5m": token_count(values.get("ephemeral_5m_input_tokens")),
            "1h": token_count(values.get("ephemeral_1h_input_tokens")),
        }
        return revised(row, {"cache_creation_by_ttl": ttl})

    def compare_calls(self) -> None:
        for call in self.ledger.rows():
            row = self.final.get(call.model)
            if row is None or any(
                row_counts(call)[field] > row_counts(row)[field]
                for field in FINAL_FIELDS
            ):
                self.observation_gap = True
                self.final[call.model] = call
            elif "reasoning_tokens" in call.known_token_fields and all(
                row_counts(call)[field] == row_counts(row)[field]
                for field in FINAL_FIELDS
            ):
                self.final[call.model] = revised(
                    row,
                    {
                        "reasoning_tokens": call.reasoning_tokens,
                        "known_token_fields": row.known_token_fields
                        | {"reasoning_tokens"},
                    },
                )

    def summary(self) -> UsageSummary:
        self.finish()
        rows = (
            dict(self.final)
            if self.final
            else {row.model: row for row in self.ledger.rows()}
        )
        for tail in self.tail.rows():
            previous = rows.get(tail.model)
            if previous is None:
                rows[tail.model] = tail
                continue
            counts: dict[str, int] = {
                field: row_counts(previous)[field] + row_counts(tail)[field]
                for field in row_counts(previous)
            }
            if any(value > MAX_TOKEN for value in counts.values()):
                self.observation_gap = True
                continue
            rows[tail.model] = revised(
                previous,
                {
                    **counts,
                    "known_token_fields": previous.known_token_fields
                    & tail.known_token_fields,
                    "cache_creation_by_ttl": None,
                    "completeness": "partial",
                    "observation_id": tail.observation_id,
                    "observation_sequence": tail.observation_sequence,
                    "observed_at": tail.observed_at,
                },
            )
        attribution = (
            self.attribution_gap
            or self.ledger.attribution_gap
            or self.tail.attribution_gap
        )
        gap = (
            self.observation_gap
            or self.ledger.observation_gap
            or self.tail.observation_gap
        )
        complete = (
            self.result_seen
            and bool(self.final)
            and not (self.aborted or self.tail.latest or attribution or gap)
        )
        state: Completeness = (
            "complete" if complete else "partial" if rows else "unavailable"
        )
        return UsageSummary(
            tuple(
                revised(row, {"completeness": state}) for _, row in sorted(rows.items())
            ),
            state,
            attribution,
            gap,
            self.observed_at,
        )
