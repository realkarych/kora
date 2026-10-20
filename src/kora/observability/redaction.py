import re
import threading
from dataclasses import dataclass

from kora.observability.settings import (
    MAX_SECRET_BYTES,
    MAX_SECRETS,
    MIN_SECRET_LENGTH,
)

MASK: str = "[redacted]"


class SecretAdmissionError(ValueError):
    pass


@dataclass(frozen=True)
class RegistryLimits:
    max_values: int = MAX_SECRETS
    max_bytes: int = MAX_SECRET_BYTES
    min_length: int = MIN_SECRET_LENGTH


DEFAULT_LIMITS: RegistryLimits = RegistryLimits()


@dataclass(frozen=True)
class Matcher:
    pattern: re.Pattern[str]
    longest: int


def mask_prefix(matcher: Matcher, text: str, retained: int) -> str:
    window = retained + matcher.longest
    tail = matcher.longest - 1
    parts: list[str] = []
    produced = 0
    position = 0
    while position < len(text) and produced <= retained:
        end = min(len(text), position + window)
        chunk = text[position:end]
        final = end == len(text)
        boundary = len(chunk) - tail
        consumed = 0
        for match in matcher.pattern.finditer(chunk):
            if not final and match.start() >= boundary:
                break
            parts.append(chunk[consumed : match.start()])
            parts.append(MASK)
            produced += match.start() - consumed + len(MASK)
            consumed = match.end()
        safe_end = len(chunk) if final else max(consumed, boundary)
        parts.append(chunk[consumed:safe_end])
        produced += safe_end - consumed
        position += safe_end
    return "".join(parts)


class SecretRegistration:
    def __init__(self, registry: "SecretRegistry", handle: int) -> None:
        self._registry = registry
        self._handle = handle

    def release(self) -> None:
        self._registry.release(self._handle)


class SecretRegistry:
    def __init__(self, limits: RegistryLimits = DEFAULT_LIMITS) -> None:
        self._limits = limits
        self._lock = threading.Lock()
        self._values: dict[int, str] = {}
        self._next_handle = 1
        self._retained_bytes = 0
        self._matcher: Matcher | None = None

    def register(self, value: str) -> SecretRegistration:
        size = len(value.encode("utf-8"))
        if size < self._limits.min_length:
            raise SecretAdmissionError("a secret shorter than the minimum is refused")
        with self._lock:
            if len(self._values) >= self._limits.max_values:
                raise SecretAdmissionError("the secret registry is full")
            if self._retained_bytes + size > self._limits.max_bytes:
                raise SecretAdmissionError("the secret registry byte bound is reached")
            handle = self._next_handle
            self._next_handle += 1
            self._values[handle] = value
            self._retained_bytes += size
            self._recompile()
        return SecretRegistration(self, handle)

    def release(self, handle: int) -> None:
        with self._lock:
            value = self._values.pop(handle, None)
            if value is None:
                return
            self._retained_bytes -= len(value.encode("utf-8"))
            self._recompile()

    def _recompile(self) -> None:
        if not self._values:
            self._matcher = None
            return
        literals = sorted(set(self._values.values()), key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(literal) for literal in literals))
        self._matcher = Matcher(pattern, len(literals[0]))
        re.purge()

    def mask(self, text: str, retained: int | None = None) -> str:
        matcher = self._matcher
        if matcher is None:
            return text
        if retained is None or len(text) <= retained + matcher.longest:
            return matcher.pattern.sub(MASK, text)
        return mask_prefix(matcher, text, retained)

    def count(self) -> int:
        with self._lock:
            return len(self._values)

    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes
