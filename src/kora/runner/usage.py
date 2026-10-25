from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from kora.ir.cost import (
    MAX_TOKEN,
    Completeness,
    TokenField,
    UsageCoverage,
    UsageRow,
    format_money,
    parse_money,
)
from kora.protocols.harness import UsageData

MAX_OPEN_MESSAGES: Final[int] = 1_024
MAX_CLOSED_MESSAGES: Final[int] = 1_024
MAX_MODELS: Final[int] = 64
ORDER: Final[Mapping[Completeness, int]] = {
    "unavailable": 0,
    "partial": 1,
    "complete": 2,
}


def weakest(states: Iterable[Completeness]) -> Completeness:
    result: Completeness = "complete"
    for state in states:
        if ORDER[state] < ORDER[result]:
            result = state
    return result


@dataclass(frozen=True)
class Totals:
    tokens: tuple[int, int, int, int, int]
    known: frozenset[TokenField]
    ttl: tuple[int, int] | None
    reported_usd: Decimal | None
    completeness: Completeness
    last: UsageData

    @staticmethod
    def of(data: UsageData) -> "Totals":
        split = data.cache_creation_by_ttl
        return Totals(
            tokens=(
                data.input_tokens,
                data.cached_input_tokens,
                data.cache_creation_tokens,
                data.output_tokens,
                data.reasoning_tokens,
            ),
            known=data.known_token_fields,
            ttl=None if split is None else (split["5m"], split["1h"]),
            reported_usd=(
                None if data.reported_usd is None else parse_money(data.reported_usd)
            ),
            completeness=data.coverage.completeness,
            last=data,
        )

    def plus(self, other: "Totals") -> "Totals":
        latest = max(
            (self.last, other.last), key=lambda item: item.observation_sequence
        )
        return Totals(
            tokens=(
                self.tokens[0] + other.tokens[0],
                self.tokens[1] + other.tokens[1],
                self.tokens[2] + other.tokens[2],
                self.tokens[3] + other.tokens[3],
                self.tokens[4] + other.tokens[4],
            ),
            known=self.known & other.known,
            ttl=(
                None
                if self.ttl is None or other.ttl is None
                else (self.ttl[0] + other.ttl[0], self.ttl[1] + other.ttl[1])
            ),
            reported_usd=(
                None
                if self.reported_usd is None or other.reported_usd is None
                else self.reported_usd + other.reported_usd
            ),
            completeness=weakest((self.completeness, other.completeness)),
            last=latest,
        )


class UsageLedger:
    def __init__(self) -> None:
        self.high_water: int | None = None
        self.open: OrderedDict[str, UsageData] = OrderedDict()
        self.closed_ids: OrderedDict[str, None] = OrderedDict()
        self.closed: dict[str, Totals] = {}
        self.models: set[str] = set()
        self.latest: UsageData | None = None
        self.attribution_gap = False
        self.observation_gap = False
        self.overflowed = False

    def observe(self, data: UsageData) -> None:
        if self.high_water is not None and data.observation_sequence <= self.high_water:
            if data.observation_sequence == self.high_water and data != self.latest:
                self.observation_gap = True
            return
        self.high_water = data.observation_sequence
        self.latest = data
        self.attribution_gap |= data.coverage.attribution_gap
        self.observation_gap |= data.coverage.observation_gap
        if data.model is None:
            self.attribution_gap = True
            return
        if data.model not in self.models and len(self.models) >= MAX_MODELS:
            self.attribution_gap = True
            return
        if data.message_id in self.closed_ids:
            self.observation_gap = True
            return
        if data.message_id not in self.open:
            if self.overflowed:
                self.observation_gap = True
                return
            if len(self.open) >= MAX_OPEN_MESSAGES:
                self.overflowed = True
                self.observation_gap = True
                self.close(next(iter(self.open)))
        self.models.add(data.model)
        self.open[data.message_id] = data
        self.open.move_to_end(data.message_id)

    def close(self, message_id: str) -> None:
        data = self.open.pop(message_id, None)
        if data is None or data.model is None:
            return
        self.closed_ids[message_id] = None
        if len(self.closed_ids) > MAX_CLOSED_MESSAGES:
            self.closed_ids.popitem(last=False)
        current = self.closed.get(data.model)
        totals = Totals.of(data)
        self.closed[data.model] = totals if current is None else current.plus(totals)

    def model_totals(self) -> dict[str, Totals]:
        merged = dict(self.closed)
        for data in self.open.values():
            if data.model is None:
                continue
            totals = Totals.of(data)
            previous = merged.get(data.model)
            merged[data.model] = totals if previous is None else previous.plus(totals)
        return merged

    def rows(self) -> tuple[UsageRow, ...]:
        rows: list[UsageRow] = []
        for model, totals in sorted(self.model_totals().items()):
            row = self.row(model, totals)
            if row is None:
                self.observation_gap = True
            else:
                rows.append(row)
        return tuple(rows)

    def row(self, model: str, totals: Totals) -> UsageRow | None:
        if any(count > MAX_TOKEN for count in totals.tokens):
            return None
        last = totals.last
        known_ttl = totals.ttl is not None and "cache_creation_tokens" in totals.known
        return UsageRow(
            model=model,
            input_tokens=totals.tokens[0],
            cached_input_tokens=totals.tokens[1],
            cache_creation_tokens=totals.tokens[2],
            output_tokens=totals.tokens[3],
            reasoning_tokens=totals.tokens[4],
            known_token_fields=totals.known,
            cache_creation_by_ttl=(
                {"5m": totals.ttl[0], "1h": totals.ttl[1]}
                if known_ttl and totals.ttl is not None
                else None
            ),
            reported_usd=(
                None
                if totals.reported_usd is None
                else format_money(totals.reported_usd)
            ),
            observation_id=last.observation_id,
            observation_sequence=last.observation_sequence,
            observed_at=last.observed_at,
            completeness=totals.completeness,
            basis=last.basis,
            billing_class=last.billing_class,
            delta_of_session_totals=False,
            source={},
        )

    def coverage(self, result_seen: bool) -> UsageCoverage:
        rows = self.rows()
        latest = self.latest
        if latest is None:
            return UsageCoverage(
                version=1,
                observation_id=None,
                observation_sequence=None,
                completeness="unavailable",
                observed_at=None,
                attribution_gap=False,
                observation_gap=False,
            )
        return UsageCoverage(
            version=1,
            observation_id=latest.observation_id,
            observation_sequence=latest.observation_sequence,
            completeness=self.completeness(rows, result_seen),
            observed_at=latest.observed_at,
            attribution_gap=self.attribution_gap,
            observation_gap=self.observation_gap,
        )

    def completeness(
        self, rows: tuple[UsageRow, ...], result_seen: bool
    ) -> Completeness:
        if not rows:
            return "unavailable"
        state = weakest(row.completeness for row in rows)
        if state == "unavailable":
            return "unavailable"
        gap = self.attribution_gap or self.observation_gap
        if state == "complete" and result_seen and not gap:
            return "complete"
        return "partial"
