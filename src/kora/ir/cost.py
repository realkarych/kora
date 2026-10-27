import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal, Protocol, Self, TypeAlias, cast

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PlainSerializer,
    StrictBool,
    StrictStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.config import ExtraValues
from pydantic_core import PydanticCustomError

from kora.ir.json import (
    MAX_INTEGER_BITS,
    CanonicalBudget,
    JsonObject,
    JsonShapeError,
    JsonValue,
    parse_json_object,
    parse_value,
    spend_string,
)

MAX_TOKEN: int = 2_147_483_647
MAX_SUM: int = 9_007_199_254_740_991
MAX_IDENTIFIER_LENGTH: int = 256
MAX_MONEY: Decimal = Decimal("99999999999999.9999999999")
MONEY_PATTERN: re.Pattern[str] = re.compile(
    r"(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{0,9}[1-9])?"
)
TOKEN_FIELDS: frozenset[str] = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_tokens",
        "output_tokens",
        "reasoning_tokens",
    }
)
FrozenJson: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["FrozenJson", ...]
    | Mapping[str, "FrozenJson"]
)
Completeness: TypeAlias = Literal["complete", "partial", "unavailable"]
Basis: TypeAlias = Literal["harness_list", "harness_managed", "price_table", "gateway"]
BillingClass: TypeAlias = Literal["api_key", "subscription", "gateway", "managed"]
PricingStatus: TypeAlias = Literal[
    "estimated", "partially_priced", "not_billable", "unpriced"
]
TokenField: TypeAlias = Literal[
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
]


def refusal() -> PydanticCustomError:
    return PydanticCustomError("cost_invalid", "invalid accounting value")


def money(value: str) -> str:
    if len(value) > 25 or MONEY_PATTERN.fullmatch(value) is None:
        raise refusal()
    return value


class MoneyOverflow(ValueError):
    def __init__(self) -> None:
        super().__init__("accounting amount exceeds its bound")


def parse_money(value: object) -> Decimal:
    if type(value) is not str:
        raise refusal()
    return Decimal(money(value))


def format_money(value: Decimal) -> str:
    if not value.is_finite() or value < 0 or value > MAX_MONEY:
        raise MoneyOverflow()
    if value == 0:
        return "0"
    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)):
        quantized = value.quantize(Decimal("0.0000000001"))
    if value != quantized:
        raise refusal()
    return format(quantized, "f").rstrip("0").rstrip(".")


def quantize_money(value: Decimal) -> str:
    if not value.is_finite() or value < 0 or value > MAX_MONEY:
        raise MoneyOverflow()
    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)):
        quantized = value.quantize(Decimal("0.0000000001"))
    return format_money(quantized)


def identifier(value: str) -> str:
    if not value or len(value) > MAX_IDENTIFIER_LENGTH or not value.isprintable():
        raise refusal()
    return value


def timestamp_input(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as error:
            raise refusal() from error
    raise refusal()


def timestamp_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError as error:
        raise refusal() from error


def timestamp_wire(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


Money = Annotated[StrictStr, AfterValidator(money)]
Identifier = Annotated[StrictStr, AfterValidator(identifier)]
Token = Annotated[int, Field(strict=True, ge=0, le=MAX_TOKEN)]
Sum = Annotated[int, Field(strict=True, ge=0, le=MAX_SUM)]
Version = Annotated[int, Field(strict=True, ge=1, le=1)]
Timestamp = Annotated[
    AwareDatetime,
    BeforeValidator(timestamp_input),
    AfterValidator(timestamp_utc),
    PlainSerializer(timestamp_wire, return_type=str, when_used="json"),
]


def preflight(value: object, budget: CanonicalBudget, depth: int = 0) -> None:
    if depth > 16:
        raise JsonShapeError("json_depth")
    if isinstance(value, BaseModel):
        budget.spend(2 + max(len(type(value).model_fields) - 1, 0))
        for key in type(value).model_fields:
            spend_string(budget, key, "json_key", 1)
            preflight(getattr(value, key), budget, depth + 1)
    elif isinstance(value, Mapping):
        entries = cast("Mapping[object, object]", value)
        budget.spend(2 + max(len(entries) - 1, 0))
        for key, item in entries.items():
            if not isinstance(key, str):
                raise JsonShapeError("json_key")
            spend_string(budget, key, "json_key", 1)
            preflight(item, budget, depth + 1)
    elif isinstance(value, list | tuple | set | frozenset):
        items = cast(
            "list[object] | tuple[object, ...] | set[object] | frozenset[object]", value
        )
        budget.spend(2 + max(len(items) - 1, 0))
        for item in items:
            preflight(item, budget, depth + 1)
    elif isinstance(value, datetime):
        spend_string(budget, timestamp_wire(timestamp_utc(value)), "json_scalar", 0)
    else:
        if isinstance(value, int) and value.bit_length() > MAX_INTEGER_BITS:
            raise JsonShapeError("json_size")
        parse_value(value, 16, depth, budget)


def freeze_json(value: JsonValue) -> FrozenJson:
    if isinstance(value, dict):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: FrozenJson) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


class CostValue(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        arbitrary_types_allowed=True,
    )
    byte_limit: ClassVar[int] = 1_048_576

    @model_validator(mode="wrap")
    @classmethod
    def validate_owned_value(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        try:
            if isinstance(value, Mapping):
                entries = cast("Mapping[object, object]", value)
                for name in ("id", "namespace_id", "model", "observation_id"):
                    item = entries.get(name)
                    if type(item) is str and len(item) > MAX_IDENTIFIER_LENGTH:
                        raise refusal()
            preflight(cast("object", value), CanonicalBudget(cls.byte_limit))
            if isinstance(value, Mapping):
                for key in cast("Mapping[object, object]", value):
                    if not isinstance(key, str) or key not in cls.model_fields:
                        raise refusal()
            owned = handler(value)
            preflight(owned, CanonicalBudget(cls.byte_limit))
            return owned
        except (ValidationError, JsonShapeError) as error:
            raise refusal() from error

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: object = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        try:
            if (
                len(json_data) > cls.byte_limit
                or isinstance(json_data, str)
                and len(json_data.encode("utf-8")) > cls.byte_limit
            ):
                return cls.model_validate(None)
            return super().model_validate_json(
                json_data,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except (ValidationError, UnicodeEncodeError):
            return cls.model_validate(None)


class PriceRates(CostValue):
    input: Money | None
    cached_input: Money | None
    cache_creation_5m: Money | None
    cache_creation_1h: Money | None
    output: Money | None
    reasoning: Money | None


class PriceRevision(CostValue):
    byte_limit: ClassVar[int] = 65_536
    id: Identifier
    namespace_id: Identifier | None
    effective_from: Timestamp
    version: Version
    normalization_version: Version
    prices: Mapping[Identifier, PriceRates]

    @field_validator("prices", mode="after")
    @classmethod
    def own_prices(cls, value: Mapping[str, PriceRates]) -> Mapping[str, PriceRates]:
        return MappingProxyType(dict(value))

    @field_serializer("prices")
    def serialize_prices(self, value: Mapping[str, PriceRates]) -> dict[str, object]:
        return {key: item.model_dump(mode="json") for key, item in value.items()}


class UsageCoverage(CostValue):
    version: Version
    observation_id: Identifier | None
    observation_sequence: Sum | None
    completeness: Completeness
    observed_at: Timestamp | None
    attribution_gap: StrictBool
    observation_gap: StrictBool

    @model_validator(mode="after")
    def coherent_coverage(self) -> Self:
        if (self.observation_id is None) != (self.observation_sequence is None):
            raise refusal()
        if self.observation_id is None and (
            self.completeness != "unavailable"
            or self.observed_at is not None
            or self.attribution_gap
            or self.observation_gap
        ):
            raise refusal()
        if self.completeness == "complete" and (
            self.attribution_gap or self.observation_gap
        ):
            raise refusal()
        return self


def unique_sequence(value: object) -> object:
    if not isinstance(value, list | tuple | set | frozenset):
        raise refusal()
    values = cast(
        "list[object] | tuple[object, ...] | set[object] | frozenset[object]", value
    )
    if len(values) > 5 or any(not isinstance(item, str) for item in values):
        raise refusal()
    if len(set(cast("list[str]", values))) != len(values):
        raise refusal()
    return values


def sorted_fields(value: frozenset[TokenField]) -> list[TokenField]:
    return sorted(value)


KnownFields = Annotated[
    frozenset[TokenField],
    BeforeValidator(unique_sequence),
    PlainSerializer(sorted_fields, return_type=list[TokenField]),
]


def owned_ttl(
    value: Mapping[Literal["5m", "1h"], int],
) -> Mapping[Literal["5m", "1h"], int]:
    return MappingProxyType(dict(value))


TtlWire: TypeAlias = dict[Literal["5m", "1h"], Token]


def serialized_ttl(value: Mapping[Literal["5m", "1h"], int]) -> TtlWire:
    return {"5m": value["5m"], "1h": value["1h"]}


TtlSplit = Annotated[
    Mapping[Literal["5m", "1h"], Token],
    AfterValidator(owned_ttl),
    PlainSerializer(serialized_ttl, return_type=TtlWire),
]


class TokenCounts(Protocol):
    @property
    def output_tokens(self) -> int: ...

    @property
    def reasoning_tokens(self) -> int: ...

    @property
    def cache_creation_tokens(self) -> int: ...

    @property
    def known_token_fields(self) -> frozenset[TokenField]: ...

    @property
    def cache_creation_by_ttl(self) -> Mapping[Literal["5m", "1h"], int] | None: ...


def tokens_coherent(counts: TokenCounts) -> bool:
    split = counts.cache_creation_by_ttl
    return counts.reasoning_tokens <= counts.output_tokens and (
        split is None
        or (
            set(split) == {"5m", "1h"}
            and "cache_creation_tokens" in counts.known_token_fields
            and sum(split.values()) == counts.cache_creation_tokens
        )
    )


class UsageRow(CostValue):
    model: Identifier
    input_tokens: Token
    cached_input_tokens: Token
    cache_creation_tokens: Token
    output_tokens: Token
    reasoning_tokens: Token
    known_token_fields: KnownFields
    cache_creation_by_ttl: TtlSplit | None
    reported_usd: Money | None
    observation_id: Identifier
    observation_sequence: Sum
    observed_at: Timestamp
    completeness: Completeness
    basis: Basis
    billing_class: BillingClass
    delta_of_session_totals: StrictBool
    source: Mapping[str, object]

    @field_validator("source", mode="plain", json_schema_input_type=dict[str, object])
    @classmethod
    def own_source(cls, value: object) -> Mapping[str, FrozenJson]:
        preflight(value, CanonicalBudget(8192))
        parsed = parse_json_object(value, max_bytes=8192)
        return MappingProxyType(
            {key: freeze_json(item) for key, item in parsed.items()}
        )

    @field_serializer("source", return_type=dict[str, object])
    def serialize_source(self, value: Mapping[str, FrozenJson]) -> JsonObject:
        return {key: thaw_json(item) for key, item in value.items()}

    @model_validator(mode="after")
    def coherent_tokens(self) -> Self:
        if not tokens_coherent(self):
            raise refusal()
        return self


def sorted_unique(value: tuple[str, ...]) -> tuple[str, ...]:
    if tuple(sorted(set(value))) != value:
        raise refusal()
    return value


def bounded_sequence(value: object, limit: int) -> object:
    if not isinstance(value, list | tuple):
        raise refusal()
    items = cast("list[object] | tuple[object, ...]", value)
    if len(items) > limit:
        raise refusal()
    return items


def consistent_price(
    status: PricingStatus,
    total: str | None,
    known: str | None,
    classes: tuple[BillingClass, ...] = (),
) -> None:
    if status == "estimated" and (total is None or total != known):
        raise refusal()
    if status == "partially_priced" and (total is not None or known is None):
        raise refusal()
    if status in {"unpriced", "not_billable"} and (
        total is not None or known is not None
    ):
        raise refusal()
    if classes and (
        (status == "not_billable") != (classes == ("subscription",))
        or "subscription" in classes
        and status == "estimated"
    ):
        raise refusal()


class ModelCost(CostValue):
    model: Identifier
    input_tokens: Sum
    cached_input_tokens: Sum
    cache_creation_tokens: Sum
    output_tokens: Sum
    reasoning_tokens: Sum
    unknown_token_fields: tuple[TokenField, ...]
    completeness: Completeness
    usd_estimate: Money | None
    known_usd_estimate: Money | None
    pricing_status: PricingStatus
    billing_classes: tuple[BillingClass, ...] = Field(min_length=1, max_length=4)
    bases: tuple[Basis, ...] = Field(min_length=1, max_length=4)

    @field_validator("unknown_token_fields", "billing_classes", "bases", mode="before")
    @classmethod
    def bounded_sets(cls, value: object) -> object:
        return bounded_sequence(value, 5)

    @model_validator(mode="after")
    def coherent_model(self) -> Self:
        sorted_unique(self.unknown_token_fields)
        sorted_unique(self.billing_classes)
        sorted_unique(self.bases)
        if self.reasoning_tokens > self.output_tokens:
            raise refusal()
        consistent_price(
            self.pricing_status,
            self.usd_estimate,
            self.known_usd_estimate,
            self.billing_classes,
        )
        return self


class RunCost(CostValue):
    version: Version
    as_of: Timestamp
    observed_at: Timestamp | None
    completeness: Completeness
    usd_estimate: Money | None
    known_usd_estimate: Money | None
    pricing_status: PricingStatus
    missing_report_attempts: Sum
    attribution_gap_attempts: Sum
    observation_gap_attempts: Sum
    models: tuple[ModelCost, ...] = Field(max_length=1024)

    @field_validator("models", mode="before")
    @classmethod
    def bounded_models(cls, value: object) -> object:
        return bounded_sequence(value, 1024)

    @model_validator(mode="after")
    def coherent_run(self) -> Self:
        sorted_unique(tuple(item.model for item in self.models))
        consistent_price(
            self.pricing_status, self.usd_estimate, self.known_usd_estimate
        )
        gaps = bool(
            self.missing_report_attempts
            or self.attribution_gap_attempts
            or self.observation_gap_attempts
        )
        if gaps and (
            self.completeness == "complete" or self.pricing_status == "estimated"
        ):
            raise refusal()
        if self.missing_report_attempts and (
            self.completeness != "unavailable" or self.observed_at is not None
        ):
            raise refusal()
        if self.models and self.completeness == "complete" and self.observed_at is None:
            raise refusal()
        if self.observed_at is not None and self.observed_at > self.as_of:
            raise refusal()
        states = {"unavailable": 0, "partial": 1, "complete": 2}
        if any(
            states[self.completeness] > states[item.completeness]
            for item in self.models
        ):
            raise refusal()
        known = [
            Decimal(item.known_usd_estimate)
            for item in self.models
            if item.known_usd_estimate is not None
        ]
        with localcontext(Context(prec=64)):
            subtotal = sum(known, Decimal(0))
        if (
            self.known_usd_estimate is not None
            and Decimal(self.known_usd_estimate) != subtotal
        ):
            raise refusal()
        if known and self.known_usd_estimate is None:
            raise refusal()
        if not known and self.known_usd_estimate is not None and (self.models or gaps):
            raise refusal()
        if self.pricing_status == "estimated" and any(
            item.pricing_status != "estimated" for item in self.models
        ):
            raise refusal()
        if self.pricing_status == "not_billable" and (
            gaps
            or not self.models
            or any(item.pricing_status != "not_billable" for item in self.models)
        ):
            raise refusal()
        if self.models and not gaps:
            all_priced = all(item.pricing_status == "estimated" for item in self.models)
            all_subscription = all(
                item.pricing_status == "not_billable" for item in self.models
            )
            if all_priced and self.pricing_status != "estimated":
                raise refusal()
            if all_subscription and self.pricing_status != "not_billable":
                raise refusal()
        if (
            not self.models
            and not gaps
            and (
                self.completeness != "complete"
                or self.pricing_status != "estimated"
                or self.usd_estimate != "0"
                or self.observed_at is not None
            )
        ):
            raise refusal()
        return self


def accumulate_attempt_models(prior: frozenset[str], model: str) -> frozenset[str]:
    if type(prior) is not frozenset or len(prior) > 64 or type(model) is not str:
        raise refusal()
    identifier(model)
    for previous in prior:
        if (
            type(previous) is not str
            or not previous
            or len(previous) > MAX_IDENTIFIER_LENGTH
            or not previous.isprintable()
        ):
            raise refusal()
    if model not in prior and len(prior) == 64:
        raise refusal()
    return prior | {model}
