import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from kora.plugins.harness.codex.app_server_rpc import AppServerRpc, Notification

ExecutionOs = Literal["linux", "macos"]
PRODUCT_TOKEN = re.compile(r"[!#$%&'*+\-.^_\x60|~0-9A-Za-z]{1,64}")
NON_ORIGINATING_CLIENTS = frozenset({"codex_app_server_daemon", "codex-backend"})


class StartupFailure(StrEnum):
    INVALID_EXPECTATION = "startup_invalid_expectation"
    INVALID_RESPONSE = "startup_invalid_response"
    VERSION_MISMATCH = "startup_version_mismatch"
    PLATFORM_MISMATCH = "startup_platform_mismatch"
    HOME_MISMATCH = "startup_home_mismatch"


class StartupError(Exception):
    def __init__(self, reason: StartupFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class StartupIdentity:
    version: str
    platform_os: ExecutionOs
    codex_home: Path
    client_name: str
    client_version: str

    def __post_init__(self) -> None:
        if (
            not self.codex_home.is_absolute()
            or ".." in self.codex_home.parts
            or self.platform_os not in ("linux", "macos")
            or self.client_name in NON_ORIGINATING_CLIENTS
            or any(
                PRODUCT_TOKEN.fullmatch(token) is None
                for token in (self.version, self.client_name, self.client_version)
            )
        ):
            raise StartupError(StartupFailure.INVALID_EXPECTATION)


def verify_startup(response: object, expected: StartupIdentity) -> None:
    if not isinstance(response, dict):
        raise StartupError(StartupFailure.INVALID_RESPONSE)
    fields = cast("dict[str, object]", response)
    user_agent = fields.get("userAgent")
    family = fields.get("platformFamily")
    platform_os = fields.get("platformOs")
    home = fields.get("codexHome")
    if not (
        isinstance(user_agent, str)
        and isinstance(family, str)
        and isinstance(platform_os, str)
        and isinstance(home, str)
    ):
        raise StartupError(StartupFailure.INVALID_RESPONSE)
    product, separator, _ = user_agent.partition(" ")
    if not separator or product != f"{expected.client_name}/{expected.version}":
        raise StartupError(StartupFailure.VERSION_MISMATCH)
    if family != "unix" or platform_os != expected.platform_os:
        raise StartupError(StartupFailure.PLATFORM_MISMATCH)
    if home != str(expected.codex_home):
        raise StartupError(StartupFailure.HOME_MISMATCH)


async def initialize_app_server(
    rpc: AppServerRpc,
    expected: StartupIdentity,
    *,
    deadline: float,
    experimental_api: bool = False,
    observe: Callable[[Notification], bool | None] | None = None,
    before_write: Callable[[], None] | None = None,
) -> str:
    params: dict[str, object] = {
        "clientInfo": {
            "name": expected.client_name,
            "version": expected.client_version,
        }
    }
    if experimental_api:
        params["capabilities"] = {"experimentalApi": True}
    response = await rpc.call(
        "initialize",
        params,
        deadline=deadline,
        observe=observe if observe is not None else lambda _notice: None,
        before_write=before_write,
    )
    verify_startup(response, expected)
    await rpc.notify("initialized", deadline=deadline, before_write=before_write)
    return expected.version
