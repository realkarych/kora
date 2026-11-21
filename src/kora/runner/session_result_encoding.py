import heapq
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from json.encoder import encode_basestring
from threading import Event
from typing import Final, cast

from pydantic import BaseModel

from kora.ir.cost import UsageRow
from kora.ir.json import DEFAULT_MAX_DEPTH, MAX_CANONICAL_BYTES, MAX_INTEGER_BITS
from kora.protocols.harness import (
    DenialSample,
    OutputCandidate,
    SessionResult,
    UnconfirmedSample,
)
from kora.protocols.runtime import SessionObservation, SessionResultObservation

MAX_RESULT_BYTES: Final[int] = 16 * 1_048_576
SCALAR_CHARS: Final[int] = 2_048
KEY_BATCH: Final[int] = 256
USAGE_SOURCE_BYTES: Final[int] = 8_192
OUTPUT_VALUE: Final[tuple[str, ...]] = ("result", "output", "value")
USAGE_SOURCE: Final[tuple[str, ...]] = ("result", "usage", "", "source")
FIXED_FIELDS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {("kind",), ("cause",), ("result", "output", "source")}
)


class ResultTooLarge(Exception):
    def __init__(self) -> None:
        super().__init__("the session observation exceeds its size cap")


class ResultEncodingRefused(Exception):
    def __init__(self) -> None:
        super().__init__("the session observation cannot be encoded")


class ResultPreparationStopped(Exception):
    def __init__(self) -> None:
        super().__init__("session result preparation was stopped")


class ResultContainsSecret(Exception):
    def __init__(self) -> None:
        super().__init__("the session observation contains a registered value")


def check_running(stop: Event) -> None:
    if stop.is_set():
        raise ResultPreparationStopped


def string_body(value: str) -> bytes:
    try:
        return encode_basestring(value)[1:-1].encode("utf-8")
    except UnicodeEncodeError:
        raise ResultEncodingRefused from None


@dataclass(frozen=True)
class SecretSpelling:
    logical: str
    raw: bytes
    escaped: bytes


class ResultMatcher:
    def __init__(self, forms: tuple[str, ...], stop: Event) -> None:
        entries: list[SecretSpelling] = []
        for form in forms:
            check_running(stop)
            raw = form.encode("utf-8")
            entries.append(SecretSpelling(form, raw, string_body(form)))
        self.entries = tuple(entries)
        self.logical_overlap = max(
            (len(entry.logical) - 1 for entry in entries), default=0
        )
        self.wire_overlap = max(
            (max(len(entry.raw), len(entry.escaped)) - 1 for entry in entries),
            default=0,
        )

    def check(self, logical: str, wire: bytes) -> None:
        for entry in self.entries:
            if entry.logical in logical or entry.raw in wire or entry.escaped in wire:
                raise ResultContainsSecret


def document(model: BaseModel) -> Mapping[str, object]:
    if isinstance(model, SessionResultObservation):
        return {
            "kind": model.kind,
            "result": model.result,
            "cause": model.cause,
            "journal_complete": model.journal_complete,
        }
    if isinstance(model, SessionResult):
        fields: dict[str, object] = model.model_dump(
            mode="json", exclude={"output", "usage", "denials", "unconfirmed_calls"}
        )
        fields.update(
            output=model.output,
            usage=model.usage,
            denials=model.denials,
            unconfirmed_calls=model.unconfirmed_calls,
        )
        return fields
    if isinstance(model, DenialSample | UnconfirmedSample):
        return {"items": model.items, "total": model.total}
    if isinstance(model, OutputCandidate):
        return {"source": model.source, "value": model.value}
    if isinstance(model, UsageRow):
        row: dict[str, object] = model.model_dump(mode="json", exclude={"source"})
        row["source"] = model.source
        return row
    return model.model_dump(mode="json")


def key_order(values: Mapping[str, object], stop: Event) -> Iterator[str]:
    keys = iter(values)
    runs: list[list[str]] = []
    while True:
        check_running(stop)
        group = list(islice(keys, KEY_BATCH))
        if not group:
            break
        if any(type(key) is not str for key in group):
            raise ResultEncodingRefused
        group.sort()
        runs.append(group)
    if len(runs) == 1:
        yield from runs[0]
    else:
        yield from heapq.merge(*runs)


class ResultEncoder:
    def __init__(self, forms: tuple[str, ...], stop: Event) -> None:
        self.matcher = ResultMatcher(forms, stop)
        self.stop = stop
        self.buffer = bytearray()
        self.ceiling: int = MAX_RESULT_BYTES
        self.positions = 0

    def guard(self, width: int) -> None:
        if width > self.ceiling - len(self.buffer):
            raise ResultTooLarge

    def append(self, data: bytes) -> None:
        self.guard(len(data))
        self.buffer.extend(data)

    def position(self) -> None:
        self.positions += 1
        if self.positions % KEY_BATCH == 0:
            check_running(self.stop)

    def string(self, value: str, content: bool) -> None:
        self.guard(len(value) + 2)
        self.append(b'"')
        logical_tail = ""
        wire_tail = b""
        for start in range(0, len(value), SCALAR_CHARS):
            check_running(self.stop)
            part = value[start : start + SCALAR_CHARS]
            encoded = string_body(part)
            if content and self.matcher.entries:
                logical = logical_tail + part
                wire = wire_tail + encoded
                self.matcher.check(logical, wire)
                length = self.matcher.logical_overlap
                logical_tail = logical[-length:] if length else ""
                length = self.matcher.wire_overlap
                wire_tail = wire[-length:] if length else b""
            self.append(encoded)
        self.append(b'"')

    def scalar(self, value: object, content: bool) -> None:
        if isinstance(value, str):
            self.string(value, content)
            return
        if type(value) is int and value.bit_length() > MAX_INTEGER_BITS:
            raise ResultTooLarge
        if type(value) is float and not math.isfinite(value):
            raise ResultEncodingRefused
        if value is not None and type(value) not in (int, float, bool):
            raise ResultEncodingRefused
        check_running(self.stop)
        encoded = json.dumps(value, allow_nan=False).encode("ascii")
        if content and self.matcher.entries:
            self.matcher.check(encoded.decode("ascii"), encoded)
        self.append(encoded)

    def value(
        self,
        value: object,
        path: tuple[str, ...] = (),
        depth: int | None = None,
    ) -> None:
        self.position()
        previous = self.ceiling
        if path in (OUTPUT_VALUE, USAGE_SOURCE):
            depth = 0
            limit = MAX_CANONICAL_BYTES if path == OUTPUT_VALUE else USAGE_SOURCE_BYTES
            self.ceiling = min(previous, len(self.buffer) + limit)
        try:
            if isinstance(value, BaseModel):
                self.mapping(document(value), path, depth)
            elif isinstance(value, Mapping):
                self.mapping(cast(Mapping[str, object], value), path, depth)
            elif type(value) in (tuple, list):
                self.sequence(cast(Sequence[object], value), path, depth)
            else:
                self.scalar(value, depth is not None or path not in FIXED_FIELDS)
        finally:
            self.ceiling = previous

    def mapping(
        self,
        values: Mapping[str, object],
        path: tuple[str, ...],
        depth: int | None,
    ) -> None:
        if depth is not None and depth >= DEFAULT_MAX_DEPTH:
            raise ResultEncodingRefused
        self.guard(2 + 4 * len(values) + max(0, len(values) - 1))
        self.append(b"{")
        for index, key in enumerate(key_order(values, self.stop)):
            self.position()
            if index:
                self.append(b",")
            self.string(key, depth is not None)
            self.append(b":")
            self.value(
                values[key],
                (*path, key) if depth is None else (),
                None if depth is None else depth + 1,
            )
        self.append(b"}")

    def sequence(
        self,
        values: Sequence[object],
        path: tuple[str, ...],
        depth: int | None,
    ) -> None:
        if depth is not None and depth >= DEFAULT_MAX_DEPTH:
            raise ResultEncodingRefused
        self.guard(2 + len(values) + max(0, len(values) - 1))
        self.append(b"[")
        for index, value in enumerate(values):
            if index:
                self.append(b",")
            self.value(
                value,
                (*path, "") if depth is None else (),
                None if depth is None else depth + 1,
            )
        self.append(b"]")


def encode_observation(
    observation: SessionObservation, forms: tuple[str, ...], stop: Event
) -> bytearray:
    encoder = ResultEncoder(forms, stop)
    encoder.value(observation)
    check_running(stop)
    return encoder.buffer
