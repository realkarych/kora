import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TypeAlias, cast

from kora.observability.catalog import (
    CONTEXT_KEYS,
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
        budget.take_value()
        budget.take_string(len(value.encode("utf-8")))
        return truncate(escape_controls(self._registry.mask(value)))

    def safe_scalar(self, value: object, budget: Budget) -> SafeValue:
        if isinstance(value, str):
            return self.safe_string(value, budget)
        if isinstance(value, bool) or value is None:
            budget.take_value()
            return value
        if isinstance(value, int):
            budget.take_value()
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise RecordRejected(LossReason.INVALID_FIELD)
            budget.take_value()
            return value
        raise RecordRejected(LossReason.INVALID_FIELD)

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
        frames: list[SafeValue] = []
        traceback: TracebackType | None = error.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            module = traceback.tb_frame.f_globals.get("__name__")
            frames.append(
                {
                    "module": module if isinstance(module, str) else "",
                    "function": code.co_qualname,
                    "line": traceback.tb_lineno,
                }
            )
            traceback = traceback.tb_next
        kept = frames[-MAX_FRAMES:]
        for _ in range(len(kept)):
            budget.take_value()
        return {
            "type": self.safe_string(type(error).__qualname__, budget),
            "module": self.safe_string(type(error).__module__, budget),
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
            budget.take_value()
            return value
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
