import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TypeAlias, cast

from sqlalchemy import Dialect, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import TIMESTAMP as POSTGRESQL_TIMESTAMP
from sqlalchemy.types import JSON, DateTime, TypeDecorator, TypeEngine

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]

POSTGRESQL: str = "postgresql"
SQLITE: str = "sqlite"
BYTE_ORDER_COLLATION: str = "C"
TIMESTAMP_TEXT_FORMAT: str = "%Y-%m-%dT%H:%M:%S.%f+00:00"
TIMESTAMP_TEXT_LENGTH: int = 32
NULL_CHARACTER: str = "\u0000"
IDENTIFIER_LENGTH: int = 26
DIGEST_LENGTH: int = 64
NAME_LENGTH: int = 255
PATH_LENGTH: int = 2048
USD_PRECISION: int = 24
USD_SCALE: int = 10


class ValueRejected(ValueError):
    pass


def canonical_json(value: JsonValue) -> str:
    reject_null_characters(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def reject_null_characters(value: JsonValue) -> None:
    if isinstance(value, str):
        if NULL_CHARACTER in value:
            raise ValueRejected("JSON strings cannot contain the null character")
    elif isinstance(value, list):
        for item in value:
            reject_null_characters(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if NULL_CHARACTER in key:
                raise ValueRejected("JSON keys cannot contain the null character")
            reject_null_characters(item)


def parse_json(text: str) -> JsonValue:
    parsed: object = json.loads(text)
    return as_json_value(parsed)


def as_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        items = cast(list[object], value)
        return [as_json_value(item) for item in items]
    if isinstance(value, dict):
        entries = cast(dict[object, object], value)
        return {str(key): as_json_value(item) for key, item in entries.items()}
    raise ValueRejected("a JSON value is a scalar, a list or an object")


class CollatedText(TypeDecorator[str]):
    impl = String
    cache_ok = True

    def __init__(self, length: int) -> None:
        super().__init__(length)
        self.length = length

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[str]:
        if dialect.name == POSTGRESQL:
            return dialect.type_descriptor(
                String(self.length, collation=BYTE_ORDER_COLLATION)
            )
        return dialect.type_descriptor(String(self.length))

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is not None and NULL_CHARACTER in value:
            raise ValueRejected("text cannot contain the null character")
        return value

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        return value


def collated_text(length: int) -> TypeEngine[str]:
    return CollatedText(length)


def normalise_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueRejected("timestamps are timezone-aware")
    return value.astimezone(UTC)


class UtcTimestamp(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def load_dialect_impl(
        self, dialect: Dialect
    ) -> TypeEngine[str] | TypeEngine[datetime]:
        if dialect.name == SQLITE:
            return dialect.type_descriptor(String(TIMESTAMP_TEXT_LENGTH))
        return dialect.type_descriptor(POSTGRESQL_TIMESTAMP(timezone=True))

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | str | None:
        if value is None:
            return None
        moment = normalise_timestamp(value)
        if dialect.name == SQLITE:
            return moment.strftime(TIMESTAMP_TEXT_FORMAT)
        return moment

    def process_result_value(
        self, value: datetime | str | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            return datetime.strptime(value, TIMESTAMP_TEXT_FORMAT).replace(tzinfo=UTC)
        return normalise_timestamp(value)


class JsonDocument(TypeDecorator[JsonValue]):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(
        self, dialect: Dialect
    ) -> TypeEngine[str] | TypeEngine[JsonValue]:
        if dialect.name == SQLITE:
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(JSONB(none_as_null=True))

    def process_bind_param(
        self, value: JsonValue | None, dialect: Dialect
    ) -> JsonValue | str | None:
        if value is None:
            return None
        if dialect.name == SQLITE:
            return canonical_json(value)
        reject_null_characters(value)
        return value

    def process_result_value(
        self, value: object | None, dialect: Dialect
    ) -> JsonValue | None:
        if value is None:
            return None
        if isinstance(value, str) and dialect.name == SQLITE:
            return parse_json(value)
        return as_json_value(value)


class UsdAmount(TypeDecorator[Decimal]):
    impl = Numeric
    cache_ok = True

    def load_dialect_impl(
        self, dialect: Dialect
    ) -> TypeEngine[str] | TypeEngine[Decimal]:
        if dialect.name == SQLITE:
            return dialect.type_descriptor(String(USD_PRECISION + 2))
        return dialect.type_descriptor(Numeric(USD_PRECISION, USD_SCALE))

    def process_bind_param(
        self, value: Decimal | None, dialect: Dialect
    ) -> Decimal | str | None:
        if value is None:
            return None
        if not value.is_finite():
            raise ValueRejected("amounts are finite decimals")
        amount = value.quantize(Decimal(1).scaleb(-USD_SCALE))
        if dialect.name == SQLITE:
            return format(amount, "f")
        return amount

    def process_result_value(
        self, value: Decimal | str | None, dialect: Dialect
    ) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ValueRejected("a stored amount is a decimal") from error
