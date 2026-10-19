import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TypeAlias, cast

from kora.observability.catalog import (
    CONTEXT_INTEGER_KEYS,
    CONTEXT_KEYS,
    CONTEXT_STRING_KEYS,
    RESERVED_KEYS,
    EventSpec,
    FieldKind,
    LossReason,
)
from kora.observability.redaction import SecretRegistry
from kora.observability.settings import (
    MAX_CHAINED_EXCEPTIONS,
    MAX_COLLECTION_ITEMS,
    MAX_FIELDS,
    MAX_FRAMES,
    MAX_NESTING,
    MAX_STRING_BYTES,
    MAX_VALUES,
)

TRUNCATION_MARKER: str = "[truncated]"
CONTEXT_IDENTIFIER: re.Pattern[str] = re.compile(r"\A[\x21-\x7e]{1,256}\Z")
MAX_INTEGER: int = 2**63
CONTROL_CHARACTERS: frozenset[int] = frozenset(
    {*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)}
)

SafeValue: TypeAlias = (
    str | int | float | bool | None | list["SafeValue"] | dict[str, "SafeValue"]
)


class RecordRejected(Exception):
    def __init__(self, reason: LossReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass
class Budget:
    values: int
    string_bytes: int

    def take_value(self) -> None:
        self.values -= 1
        if self.values < 0:
            raise RecordRejected(LossReason.OVER_BUDGET)

    def take_string(self, size: int) -> None:
        self.string_bytes -= size
        if self.string_bytes < 0:
            raise RecordRejected(LossReason.OVER_BUDGET)


def name_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def escape_controls(text: str) -> str:
    if not any(ord(character) in CONTROL_CHARACTERS for character in text):
        return text
    return "".join(
        f"\\x{ord(character):02x}"
        if ord(character) in CONTROL_CHARACTERS
        else character
        for character in text
    )


def truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_STRING_BYTES:
        return text
    keep = encoded[: MAX_STRING_BYTES - len(TRUNCATION_MARKER)]
    return keep.decode("utf-8", errors="ignore") + TRUNCATION_MARKER


class ContentPolicy:
    def __init__(self, registry: SecretRegistry, max_record_bytes: int) -> None:
        self._registry = registry
        self._max_record_bytes = max_record_bytes

    def new_budget(self) -> Budget:
        return Budget(values=MAX_VALUES, string_bytes=self._max_record_bytes)

    def safe_string(self, value: str, budget: Budget) -> str:
        encoded = value.encode("utf-8", errors="replace")
        budget.take_value()
        budget.take_string(len(encoded))
        text = encoded.decode("utf-8")
        return truncate(escape_controls(self._registry.mask(text)))

    def safe_scalar(self, value: object, budget: Budget) -> SafeValue:
        if isinstance(value, str):
            return self.safe_string(value, budget)
        if isinstance(value, bool) or value is None:
            budget.take_value()
            return value
        if isinstance(value, int):
            return self.safe_integer(value, budget)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise RecordRejected(LossReason.INVALID_FIELD)
            budget.take_value()
            return value
        raise RecordRejected(LossReason.INVALID_FIELD)

    def safe_integer(self, value: int, budget: Budget) -> int:
        if abs(value) > MAX_INTEGER:
            raise RecordRejected(LossReason.INVALID_FIELD)
        budget.take_value()
        return value

    def safe_strings(self, value: object, budget: Budget) -> list[SafeValue]:
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise RecordRejected(LossReason.INVALID_FIELD)
        items = cast(Sequence[object], value)
        if len(items) > MAX_COLLECTION_ITEMS:
            raise RecordRejected(LossReason.OVER_BUDGET)
        result: list[SafeValue] = []
        for item in items:
            if not isinstance(item, str):
                raise RecordRejected(LossReason.INVALID_FIELD)
            result.append(self.safe_string(item, budget))
        return result

    def safe_mapping(
        self, value: object, budget: Budget, depth: int
    ) -> dict[str, SafeValue]:
        if not isinstance(value, Mapping):
            raise RecordRejected(LossReason.INVALID_FIELD)
        if depth > MAX_NESTING:
            raise RecordRejected(LossReason.OVER_BUDGET)
        mapping = cast(Mapping[object, object], value)
        if len(mapping) > MAX_COLLECTION_ITEMS:
            raise RecordRejected(LossReason.OVER_BUDGET)
        result: dict[str, SafeValue] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise RecordRejected(LossReason.INVALID_FIELD)
            result[self.safe_string(key, budget)] = self.safe_item(item, budget, depth)
        return result

    def safe_item(self, value: object, budget: Budget, depth: int) -> SafeValue:
        if isinstance(value, Mapping):
            return self.safe_mapping(
                cast(Mapping[object, object], value), budget, depth + 1
            )
        if isinstance(value, Sequence) and not isinstance(value, str):
            return self.safe_strings(cast(Sequence[object], value), budget)
        return self.safe_scalar(value, budget)

    def safe_exception(self, value: object, budget: Budget) -> list[SafeValue]:
        if not isinstance(value, BaseException):
            raise RecordRejected(LossReason.INVALID_FIELD)
        chain: list[SafeValue] = []
        current: BaseException | None = value
        seen: set[int] = set()
        while current is not None and len(chain) < MAX_CHAINED_EXCEPTIONS:
            if id(current) in seen:
                break
            seen.add(id(current))
            chain.append(self.safe_exception_entry(current, budget))
            current = current.__cause__ or (
                None if current.__suppress_context__ else current.__context__
            )
        return chain

    def safe_exception_entry(
        self, error: BaseException, budget: Budget
    ) -> dict[str, SafeValue]:
        frames: list[tuple[str, str, int]] = []
        traceback: TracebackType | None = error.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            module = traceback.tb_frame.f_globals.get("__name__")
            frames.append(
                (name_or_empty(module), code.co_qualname, traceback.tb_lineno)
            )
            traceback = traceback.tb_next
        kept: list[SafeValue] = [
            {
                "module": self.safe_string(module, budget),
                "function": self.safe_string(function, budget),
                "line": line,
            }
            for module, function, line in frames[-MAX_FRAMES:]
        ]
        error_type: object = type(error)
        return {
            "type": self.safe_string(
                name_or_empty(getattr(error_type, "__qualname__", "")), budget
            ),
            "module": self.safe_string(
                name_or_empty(getattr(error_type, "__module__", "")), budget
            ),
            "frames": kept,
        }

    def safe_field(self, kind: FieldKind, value: object, budget: Budget) -> SafeValue:
        if kind is FieldKind.STRING:
            if not isinstance(value, str):
                raise RecordRejected(LossReason.INVALID_FIELD)
            return self.safe_string(value, budget)
        if kind is FieldKind.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise RecordRejected(LossReason.INVALID_FIELD)
            return self.safe_integer(value, budget)
        if kind is FieldKind.FLOAT:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise RecordRejected(LossReason.INVALID_FIELD)
            return self.safe_scalar(float(value), budget)
        if kind is FieldKind.BOOLEAN:
            if not isinstance(value, bool):
                raise RecordRejected(LossReason.INVALID_FIELD)
            budget.take_value()
            return value
        if kind is FieldKind.STRINGS:
            return self.safe_strings(value, budget)
        if kind is FieldKind.MAPPING:
            return self.safe_mapping(value, budget, 1)
        return self.safe_exception(value, budget)

    def safe_context(self, context: Mapping[str, object]) -> dict[str, SafeValue]:
        result: dict[str, SafeValue] = {}
        for key in sorted(context):
            value = context[key]
            if key in CONTEXT_STRING_KEYS:
                if not isinstance(value, str) or not CONTEXT_IDENTIFIER.match(value):
                    raise RecordRejected(LossReason.INVALID_CONTEXT)
                if self._registry.mask(value) != value:
                    raise RecordRejected(LossReason.INVALID_CONTEXT)
                result[key] = value
            elif key in CONTEXT_INTEGER_KEYS:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RecordRejected(LossReason.INVALID_CONTEXT)
                result[key] = value
        return result

    def safe_fields(
        self, spec: EventSpec, fields: Mapping[str, object]
    ) -> dict[str, SafeValue]:
        if len(fields) > MAX_FIELDS:
            raise RecordRejected(LossReason.OVER_BUDGET)
        budget = self.new_budget()
        result: dict[str, SafeValue] = {}
        for key, value in fields.items():
            if key in CONTEXT_KEYS:
                raise RecordRejected(LossReason.CONTEXT_OVERRIDE)
            if key in RESERVED_KEYS:
                raise RecordRejected(LossReason.UNKNOWN_FIELD)
            kind = spec.fields.get(key)
            if kind is None:
                raise RecordRejected(LossReason.UNKNOWN_FIELD)
            result[key] = self.safe_field(kind, value, budget)
        return result
