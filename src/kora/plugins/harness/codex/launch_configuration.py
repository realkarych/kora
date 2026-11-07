import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Address
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

NativeSandbox = Literal["read-only", "workspace-write", "danger-full-access"]

CODEX_VERSION = "0.166.0"
MODEL_PROVIDER = "kora_responses"
PROVIDER_KEY = "CODEX_API_KEY"
BUILTIN_EXECUTOR = "CODEX_EXEC_SERVER_URL"
EXEC_METADATA_ENV_NAMES = frozenset({"CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID"})
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
PROVIDER_URL = re.compile(
    r"https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])"
    r"(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?"
)
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
BEARER_DIGEST = re.compile(r"[0-9a-f]{64}")
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")

NATIVE_SETTINGS = f"""approval_policy = "never"
allow_login_shell = false
web_search = "disabled"
cli_auth_credentials_store = "file"

[analytics]
enabled = false

[features]
hooks = true
unified_exec = false

[shell_environment_policy]
inherit = "all"
exclude = ["{PROVIDER_KEY}", "{BUILTIN_EXECUTOR}"]
"""


class LaunchConfigurationFailure(StrEnum):
    INVALID_PROVIDER = "codex_launch_invalid_provider"
    INVALID_PLACEMENT = "codex_launch_invalid_placement"
    INVALID_EXECUTOR_DIGEST = "codex_launch_invalid_executor_digest"
    INVALID_ENVIRONMENT_NAMES = "codex_launch_invalid_environment_names"
    ENVIRONMENT_CONFLICT = "codex_launch_environment_conflict"


class LaunchConfigurationError(ValueError):
    def __init__(self, reason: LaunchConfigurationFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


def valid_provider_url(value: str) -> bool:
    if len(value) > 2048 or PROVIDER_URL.fullmatch(value) is None:
        return False
    try:
        address = urlsplit(value)
        port = address.port
        host = address.hostname
        if host is None or port == 0:
            return False
        if ":" not in host:
            labels = host.split(".")
            if labels[-1].isdigit() or labels[-1].lower().startswith("0x"):
                IPv4Address(host)
            elif len(host) > 253 or any(
                DNS_LABEL.fullmatch(label) is None for label in labels
            ):
                return False
    except ValueError:
        return False
    return not {".", ".."}.intersection(address.path.split("/"))


@dataclass(frozen=True)
class CodexLaunchProfile:
    model: str = field(repr=False)
    base_url: str = field(repr=False)
    sandbox: NativeSandbox

    def __post_init__(self) -> None:
        if (
            len(self.model) > 128
            or MODEL_NAME.fullmatch(self.model) is None
            or not valid_provider_url(self.base_url)
            or self.sandbox
            not in ("read-only", "workspace-write", "danger-full-access")
        ):
            raise LaunchConfigurationError(LaunchConfigurationFailure.INVALID_PROVIDER)

    def app_server_config(self, workspace: Path) -> str:
        return f"""model = "{self.model}"
model_provider = "{MODEL_PROVIDER}"
sandbox_mode = "{self.sandbox}"
project_doc_max_bytes = 0
{NATIVE_SETTINGS}
[projects.{json.dumps(str(workspace), ensure_ascii=False)}]
trust_level = "untrusted"

[model_providers.{MODEL_PROVIDER}]
name = "kora Responses"
base_url = "{self.base_url}"
wire_api = "responses"
env_key = "{PROVIDER_KEY}"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
"""


@dataclass(frozen=True)
class NativeHomes:
    home: Path
    codex_home: Path
    temporary: Path

    def paths(self) -> tuple[Path, Path, Path]:
        return self.home, self.codex_home, self.temporary

    def assignments(self, bash: Path) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "TMPDIR": str(self.temporary),
            "SHELL": str(bash),
        }


@dataclass(frozen=True)
class NativeProcessInputs:
    argv: tuple[str, ...] = field(repr=False)
    cwd: Path
    config: str = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class CodexLaunchInputs:
    app_server: NativeProcessInputs
    exec_server: NativeProcessInputs


def valid_path(path: Path) -> bool:
    try:
        str(path).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        path.anchor == "/"
        and path != path.parent
        and ".." not in path.parts
        and CONTROL_CHARACTER.search(str(path)) is None
    )


def disjoint_paths(paths: tuple[Path, ...]) -> bool:
    return all(
        not right.is_relative_to(left) and not left.is_relative_to(right)
        for index, left in enumerate(paths)
        for right in paths[index + 1 :]
    )


def build_launch_inputs(
    profile: CodexLaunchProfile,
    *,
    executable: Path,
    bash: Path,
    workspace: Path,
    app_homes: NativeHomes,
    exec_homes: NativeHomes,
    executor_digest: str,
    workload_names: Collection[str] = (),
    workload_secret_names: Collection[str] = (),
) -> CodexLaunchInputs:
    private_paths = (workspace, *app_homes.paths(), *exec_homes.paths())
    if not all(
        valid_path(path) for path in (executable, bash, *private_paths)
    ) or not disjoint_paths(private_paths):
        raise LaunchConfigurationError(LaunchConfigurationFailure.INVALID_PLACEMENT)
    if len(executor_digest) != 64 or BEARER_DIGEST.fullmatch(executor_digest) is None:
        raise LaunchConfigurationError(
            LaunchConfigurationFailure.INVALID_EXECUTOR_DIGEST
        )
    app_env = app_homes.assignments(bash)
    exec_env = exec_homes.assignments(bash)
    app_env[BUILTIN_EXECUTOR] = "none"
    if isinstance(workload_names, str) or isinstance(workload_secret_names, str):
        raise LaunchConfigurationError(
            LaunchConfigurationFailure.INVALID_ENVIRONMENT_NAMES
        )
    owned = app_env.keys() | exec_env.keys() | {PROVIDER_KEY} | EXEC_METADATA_ENV_NAMES
    if not owned.isdisjoint(workload_names) or not owned.isdisjoint(
        workload_secret_names
    ):
        raise LaunchConfigurationError(LaunchConfigurationFailure.ENVIRONMENT_CONFLICT)
    return CodexLaunchInputs(
        app_server=NativeProcessInputs(
            argv=(
                str(executable),
                "app-server",
                "--listen",
                "stdio://",
                "--strict-config",
            ),
            cwd=workspace,
            config=profile.app_server_config(workspace),
            environment=MappingProxyType(app_env),
        ),
        exec_server=NativeProcessInputs(
            argv=(
                str(executable),
                "exec-server",
                "--strict-config",
                "--listen",
                "ws://127.0.0.1:0",
                "--ws-auth",
                "capability-token",
                "--ws-token-sha256",
                executor_digest,
            ),
            cwd=workspace,
            config=NATIVE_SETTINGS,
            environment=MappingProxyType(exec_env),
        ),
    )
