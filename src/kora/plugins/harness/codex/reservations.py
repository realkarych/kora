import re
from typing import Final

from kora.plugins.harness.codex.launch_configuration import OWNED_ENV_NAMES

NATIVE_RESERVATIONS: Final[tuple[str, ...]] = (
    "CODEX_*",
    "OPENAI_*",
    "KORA_*",
    "RUST_*",
    "BASH_FUNC_*",
    "LD_*",
    "DYLD_*",
    "PATH",
    "PWD",
    "TRACEPARENT",
    "TRACESTATE",
    "ENV",
)
RESERVATION: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\*?")


def reserved_environment() -> tuple[str, ...]:
    names = set(NATIVE_RESERVATIONS) | OWNED_ENV_NAMES
    if any(RESERVATION.fullmatch(name) is None for name in names):
        raise ValueError("the Codex environment reservation is invalid")
    return tuple(sorted(names))


RESERVED_ENV: Final[tuple[str, ...]] = reserved_environment()
