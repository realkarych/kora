import re
from itertools import product
from typing import Final

LAUNCH_ENV_NAMES: Final[tuple[str, ...]] = (
    "HOME",
    "TMPDIR",
    "CLAUDE_CONFIG_DIR",
    "SHELL",
    "ENABLE_CLAUDEAI_MCP_SERVERS",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
    "CLAUDE_CODE_MAX_RETRIES",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SDK_READS_SESSION_STATE",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_AUTOUPDATER",
)
PROXY_NAMES: Final[tuple[str, ...]] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)
NATIVE_RESERVATIONS: Final[tuple[str, ...]] = (
    "ANTHROPIC_*",
    "CLAUDE_*",
    "KORA_*",
    "NODE_*",
    "BUN_*",
    "BASH_FUNC_*",
    "LD_*",
    "DYLD_*",
    "PATH",
    "PWD",
    "TRACEPARENT",
    "TRACESTATE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "BASH_ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "PS4",
    "ENV",
)
RESERVATION: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\*?")


def reserved_environment() -> tuple[str, ...]:
    names: set[str] = set(NATIVE_RESERVATIONS) | set(LAUNCH_ENV_NAMES)
    for name in PROXY_NAMES:
        letters = [
            (letter.lower(), letter.upper()) if letter.isalpha() else (letter,)
            for letter in name
        ]
        names.update("".join(spelling) for spelling in product(*letters))
    if any(RESERVATION.fullmatch(name) is None for name in names):
        raise ValueError("the Claude environment reservation is invalid")
    return tuple(sorted(names))


RESERVED_ENV: Final[tuple[str, ...]] = reserved_environment()
