import json

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from kora.db.types import JsonValue, ValueRejected, parse_json


def source_text(value: JsonValue, *, ascii_only: bool = True) -> str:
    return json.dumps(
        value,
        ensure_ascii=ascii_only,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class UsageSourceText(TypeDecorator[JsonValue]):
    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: JsonValue | None, dialect: Dialect
    ) -> str | None:
        return None if value is None else source_text(value)

    def process_result_value(self, value: object, dialect: Dialect) -> JsonValue:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueRejected("the saved usage source is not JSON text")
        return parse_json(value)
