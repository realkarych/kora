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
        self._pattern: re.Pattern[str] | None = None

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
            self._pattern = None
            return
        literals = sorted(set(self._values.values()), key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(literal) for literal in literals))
        re.purge()

    def mask(self, text: str) -> str:
        pattern = self._pattern
        if pattern is None:
            return text
        return pattern.sub(MASK, text)

    def count(self) -> int:
        with self._lock:
            return len(self._values)

    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes
