import heapq
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from kora.ir.diagnostics import MAX_INTEGER, Diagnostic, SourceLocation

MAX_DIAGNOSTICS: int = 1_000
MAX_DIAGNOSTIC_BYTES: int = 1_048_576
EMPTY_BYTES: int = len(b'{"errors":[],"warnings":[]}')
Severity = Literal["error", "warning"]


class DiagnosticCollectionError(ValueError):
    kind: Literal["count_overflow"] = "count_overflow"

    def __init__(self) -> None:
        super().__init__("Diagnostic count exceeds the supported range.")


def increment_count(value: int) -> int:
    if value >= MAX_INTEGER:
        raise DiagnosticCollectionError
    return value + 1


def copy_diagnostic(value: Diagnostic) -> Diagnostic:
    try:
        location = value.location
        if location is not None and not isinstance(location, SourceLocation):
            raise ValueError("Diagnostic fields are invalid.")
        return Diagnostic(
            code=value.code,
            message=value.message,
            path=value.path,
            location=None
            if location is None
            else SourceLocation(file=location.file, line=location.line),
            offset=value.offset,
            node=value.node,
            hint=value.hint,
        )
    except (AttributeError, ValidationError):
        pass
    raise ValueError("Diagnostic fields are invalid.")


def encoded_diagnostic(value: Diagnostic) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, order=True)
class DiagnosticOrder:
    path: tuple[int, tuple[tuple[int, int, str], ...]]
    offset: tuple[bool, int]
    code: str
    source: tuple[bool, str, int]
    node: tuple[bool, str]
    message: str
    hint: tuple[bool, str]
    ordinal: int


def diagnostic_order(value: Diagnostic, ordinal: int) -> DiagnosticOrder:
    location = value.location
    return DiagnosticOrder(
        (0, ())
        if value.path is None
        else (
            1,
            tuple(
                (0, part, "") if isinstance(part, int) else (1, 0, part)
                for part in value.path
            ),
        ),
        (value.offset is not None, value.offset or 0),
        value.code,
        (
            location is not None,
            "" if location is None else location.file,
            0 if location is None else location.line,
        ),
        (value.node is not None, value.node or ""),
        value.message,
        (value.hint is not None, value.hint or ""),
        ordinal,
    )


@dataclass(frozen=True)
class Candidate:
    order: DiagnosticOrder
    diagnostic: Diagnostic
    size: int

    def __lt__(self, other: "Candidate") -> bool:
        return self.order > other.order


class DiagnosticPrefix:
    def __init__(self) -> None:
        self.entries: list[Candidate] = []
        self.weight: int = 0
        self.cutoff: DiagnosticOrder | None = None
        self.count: int = 0

    def add(self, diagnostic: Diagnostic, ordinal: int) -> None:
        self.count = increment_count(self.count)
        order = diagnostic_order(diagnostic, ordinal)
        if self.cutoff is not None and order >= self.cutoff:
            return
        entry = Candidate(order, diagnostic, len(encoded_diagnostic(diagnostic)))
        heapq.heappush(self.entries, entry)
        self.weight += entry.size + 1
        while (
            len(self.entries) > MAX_DIAGNOSTICS
            or EMPTY_BYTES + self.weight - 1 > MAX_DIAGNOSTIC_BYTES
        ):
            removed = heapq.heappop(self.entries)
            self.weight -= removed.size + 1
            if self.cutoff is None or removed.order < self.cutoff:
                self.cutoff = removed.order

    def ordered(self) -> list[Candidate]:
        return sorted(self.entries, key=lambda entry: entry.order)


@dataclass(frozen=True)
class DiagnosticCollection:
    errors: tuple[Diagnostic, ...]
    warnings: tuple[Diagnostic, ...]
    errors_omitted: int
    warnings_omitted: int

    @property
    def valid(self) -> bool:
        return not self.errors and self.errors_omitted == 0


class DiagnosticCollector:
    def __init__(self) -> None:
        self.errors = DiagnosticPrefix()
        self.warnings = DiagnosticPrefix()
        self.count: int = 0

    def add(self, diagnostic: Diagnostic, severity: Severity = "error") -> None:
        if severity not in ("error", "warning"):
            raise ValueError("Diagnostic severity is invalid.")
        copied = copy_diagnostic(diagnostic)
        ordinal = increment_count(self.count)
        prefix = self.errors if severity == "error" else self.warnings
        prefix.add(copied, ordinal)
        self.count = ordinal

    def finish(self) -> DiagnosticCollection:
        errors = self.errors.ordered()
        warnings: list[Candidate] = []
        size = (
            EMPTY_BYTES + sum(entry.size for entry in errors) + max(0, len(errors) - 1)
        )
        if len(errors) == min(self.errors.count, MAX_DIAGNOSTICS):
            for entry in self.warnings.ordered():
                added = entry.size + bool(warnings)
                if size + added > MAX_DIAGNOSTIC_BYTES:
                    break
                warnings.append(entry)
                size += added
        return DiagnosticCollection(
            tuple(entry.diagnostic for entry in errors),
            tuple(entry.diagnostic for entry in warnings),
            self.errors.count - len(errors),
            self.warnings.count - len(warnings),
        )
